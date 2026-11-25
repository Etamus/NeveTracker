"""Shared backoff for store refusals, independent from a valid account login."""
from datetime import datetime,timedelta,timezone
import json
from services.marketplace_session import configuration


def path(store):
    profile,_,_,_=configuration(store)
    return profile.parent/(store+'-access-backoff.json')


def limited(store):
    try:
        state=json.loads(path(store).read_text(encoding='utf-8'))
        if datetime.fromisoformat(state['retry_at'])>datetime.now(timezone.utc):
            return {'status':'RATE_LIMITED','reason':state['reason'],'retry_at':state['retry_at']}
    except (OSError,ValueError,TypeError,KeyError):pass
    return None


def record_limit(store,seconds=180):
    state={'retry_at':(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),
           'reason':'A conta está conectada, mas a loja recusou as consultas. A coleta será retomada após a pausa do site.'}
    file=path(store);file.parent.mkdir(parents=True,exist_ok=True);file.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
    return state
