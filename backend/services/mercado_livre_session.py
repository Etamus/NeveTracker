"""User initiated local browser validation; credentials never enter the app."""

from threading import Lock, Thread
from datetime import datetime, timezone
import json
import time
from urllib.parse import urlparse

from services.mercado_livre_browser import BROWSER_SLOT, PROFILE_DIR


_lock = Lock()
_state = {'status': 'idle', 'message': ''}
VALIDATION_METADATA = PROFILE_DIR.parent / 'mercado-livre-validation.json'


def validated_after(when):
    try:
        data = json.loads(VALIDATION_METADATA.read_text(encoding='utf-8'))
        verified = datetime.fromisoformat(data['verified_at']).astimezone(timezone.utc).replace(tzinfo=None)
        return when is not None and verified > when
    except (OSError, ValueError, KeyError, TypeError):
        return False


def session_status():
    with _lock:
        return dict(_state)


def _set_status(status, message):
    with _lock:
        _state.update(status=status, message=message)


def start_validation():
    with _lock:
        if _state['status'] in {'opening', 'waiting'}:
            return dict(_state)
        if not BROWSER_SLOT.acquire(blocking=False):
            raise ValueError('Uma coleta do Mercado Livre esta em andamento. Aguarde para validar o navegador.')
        _state.update(status='opening', message='Abrindo a janela do Mercado Livre.')
    Thread(target=_validate, name='mercado-livre-validation', daemon=True).start()
    return session_status()


def _validate():
    from patchright.sync_api import sync_playwright
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as runtime:
            # This visible interactive window is opened only by the user's button.
            context = runtime.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False, no_viewport=True,
                locale='pt-BR', timezone_id='America/Sao_Paulo', timeout=10000,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto('https://lista.mercadolivre.com.br/w10531315', wait_until='domcontentloaded', timeout=20000)
                _set_status('waiting', 'Conclua a verificacao na janela do Mercado Livre. Se o site solicitar, acesse sua conta diretamente nele.')
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    pages = [current for current in context.pages if not current.is_closed()]
                    if not pages:
                        _set_status('closed', 'Janela fechada. A sessao local foi preservada.')
                        return
                    for current in pages:
                        if urlparse(current.url).hostname == 'lista.mercadolivre.com.br' and current.locator('.poly-card, .ui-search-layout__item').count():
                            VALIDATION_METADATA.write_text(json.dumps({'verified_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
                            _set_status('connected', 'Acesso validado. Atualize o SKU novamente.')
                            return
                    page.wait_for_timeout(500)
                _set_status('closed', 'Tempo de validacao encerrado. A sessao local foi preservada.')
            finally:
                context.close()
    except Exception:
        _set_status('error', 'Nao foi possivel concluir a validacao. Feche outras janelas de validacao e tente novamente.')
    finally:
        BROWSER_SLOT.release()
