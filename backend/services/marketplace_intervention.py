"""Durable, store-specific access waits. No passwords or cookies are stored here."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock
import json
import uuid

STORES=('mercado_livre','shopee')
MAX_TRANSPORT_RETRIES = 5
_lock=RLock()


def _path(store,kind):
    from services.marketplace_session import configuration
    if store not in STORES:raise ValueError('Loja sem sessão de navegador.')
    profile,_,_,_=configuration(store)
    return profile.parent/f'{store}-{kind}.json'


def _read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,TypeError):return {}


def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temporary.replace(path)


def enabled(store):
    return bool(_read(_path(store,'account-config')).get('enabled',True))


def configure(store,value):
    with _lock:_write(_path(store,'account-config'),{'enabled':bool(value),'changed_at':datetime.now(timezone.utc).isoformat()})
    _wake_queue()
    return {'enabled':enabled(store)}


def pending(store):
    if store not in STORES or not enabled(store):return None
    value=_read(_path(store,'access-request'))
    return value if value.get('status')=='waiting' else None


def suspension(store):
    access=pending(store)
    if access or store not in STORES or not enabled(store):return access
    value=_read(_path(store,'access-request'))
    try:
        if value.get('status')=='retrying' and datetime.fromisoformat(value['retry_at'])>datetime.now(timezone.utc):return value
    except (ValueError,KeyError,TypeError):pass
    return None


def defer_access(store,sku,reason,seconds=20):
    """Transport failures retry on their own; they do not request login."""
    if store not in STORES or not enabled(store):return None
    with _lock:
        existing=suspension(store)
        if existing:return existing
        previous=_read(_path(store,'access-request'))
        attempts=int(previous.get('attempts',0))+1 if previous.get('status')=='retrying' else 1
        delay=min(300,seconds*2**min(attempts-1,4))
        value={'store':store,'status':'retrying','sku':str(sku or ''),'reason':str(reason or 'Falha temporária de transporte.')[:500],
               'retry_at':(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(),'attempts':attempts}
        _write(_path(store,'access-request'),value)
        return value


def require_access(store,sku,reason,url=None):
    if store not in STORES or not enabled(store):return None
    with _lock:
        existing=pending(store)
        if existing:return existing
        value={'store':store,'status':'waiting','sku':str(sku or ''),'reason':str(reason or 'A loja solicitou verificação de acesso.')[:500],
               'requested_at':datetime.now(timezone.utc).isoformat(),'url':url}
        _write(_path(store,'access-request'),value)
        return value


def release(store,target_id=None):
    """Called only after a real search/product page was verified, not on a click."""
    from services.marketplace_session import metadata_file
    stamp=datetime.now(timezone.utc).isoformat()
    with _lock:
        _write(metadata_file(store),{'verified_at':stamp,'search_target_id':target_id})
        _write(_path(store,'access-request'),{'store':store,'status':'verified','verified_at':stamp})
        from services.marketplace_access import path
        path(store).unlink(missing_ok=True)
    from services.browser_health import record_browser_result
    hosts=['www.mercadolivre.com.br','lista.mercadolivre.com.br','produto.mercadolivre.com.br'] if store=='mercado_livre' else ['shopee.com.br','www.shopee.com.br']
    for host in hosts:record_browser_result('https://'+host,'OK')
    _wake_queue()


def _wake_queue():
    from services.extraction_queue import _wake_event
    _wake_event.set()


def waiting_stores(keys):
    return [store for store in STORES if store in keys and suspension(store)]


def ready(store):
    value=_read(_path(store,'access-request'))
    return (not enabled(store) or value.get('status') in {'verified', 'transport_recovered'}
            or value.get('status') == 'retrying' and not suspension(store))


def transport_retries_exhausted(store):
    if not enabled(store):
        return False
    value = _read(_path(store, 'access-request'))
    attempts = value.get('attempts')
    return value.get('status') == 'retrying' and isinstance(attempts, int) and attempts >= MAX_TRANSPORT_RETRIES


def record_transport_recovery(store):
    if store not in STORES:
        return
    with _lock:
        path = _path(store, 'access-request')
        value = _read(path)
        if value.get('status') == 'retrying' and not suspension(store):
            _write(path, {'store': store, 'status': 'transport_recovered',
                          'transport_recovered_at': datetime.now(timezone.utc).isoformat()})


def released_after(store,when):
    from services.marketplace_session import validated_after
    if enabled(store):
        value=_read(_path(store,'access-request'))
        try:
            if value.get('status') == 'transport_recovered':
                stamp = datetime.fromisoformat(value['transport_recovered_at']).astimezone(timezone.utc).replace(tzinfo=None)
                if when is not None and stamp > when:
                    return True
            stamp=datetime.fromisoformat(value['retry_at']).astimezone(timezone.utc).replace(tzinfo=None)
            if value.get('status')=='retrying' and datetime.utcnow()>=stamp and when is not None and stamp>when:return True
        except (KeyError,ValueError,TypeError):pass
    if enabled(store):return validated_after(store,when)
    try:
        stamp=datetime.fromisoformat(_read(_path(store,'account-config'))['changed_at']).astimezone(timezone.utc).replace(tzinfo=None)
        return when is not None and stamp>when
    except (KeyError,ValueError,TypeError):return False
