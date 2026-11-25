"""Shared persisted CEP selection; no state-level substitution of quotes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, RLock
import re
from cachetools import TTLCache, cached

import requests
from models.product import LeadTimeDestination
from services.lead_time_destinations import CAPITALS, BY_UF


def destinations(db, custom=True):
    rows = db.query(LeadTimeDestination).order_by(LeadTimeDestination.position).all() if custom else []
    if not rows:
        return [{**dest, 'key': dest['uf']} for dest in CAPITALS]
    return [{**BY_UF[row.uf], 'key': row.postal_code, 'postal_code': row.postal_code,
             'capital': row.city, 'city': row.city, 'custom': True} for row in rows]


_postal_cache = TTLCache(maxsize=2048, ttl=86400)
_postal_condition = Condition(RLock())


class PostalCodeNotFound(ValueError):
    pass


def resolve_cep(cep):
    # City/UF are address metadata, not a shipping quotation. Sharing them
    # avoids another ViaCEP request for every SKU at the same destination.
    destination = _resolve_cep_cached(cep)
    if destination is None:
        raise PostalCodeNotFound(f'CEP {cep} não encontrado.')
    return dict(destination)


def first_quotable_cep(postal_codes):
    """Keep order and skip only destinations explicitly rejected by ViaCEP."""
    for cep in postal_codes:
        try:
            resolve_cep(cep)
        except PostalCodeNotFound:
            continue
        except (requests.RequestException, ValueError):
            # An address lookup failure does not stop a store from quoting.
            pass
        return cep
    return None


@cached(_postal_cache, condition=_postal_condition)
def _resolve_cep_cached(cep):
    known = next((dest for dest in CAPITALS if dest['postal_code'] == cep), None)
    if known:
        return {'postal_code': cep, 'uf': known['uf'], 'city': known['capital']}
    response = requests.get(f'https://viacep.com.br/ws/{cep}/json/', timeout=(4, 8))
    response.raise_for_status()
    data = response.json()
    if data.get('erro'):
        return None  # Cache an explicit negative result, not a transport failure.
    if data.get('uf') not in BY_UF or not data.get('localidade'):
        raise ValueError(f'CEP {cep} não encontrado.')
    return {'postal_code': cep, 'uf': data['uf'], 'city': data['localidade']}


def save_destinations(db, ceps, *, skip_unknown=False):
    normalized = []
    for value in ceps:
        value = str(value).strip()
        if not re.fullmatch(r'\d{5}-?\d{3}', value):
            raise ValueError(f'CEP inválido: {value}. Informe oito dígitos.')
        value = value.replace('-', '')
        if value not in normalized:
            normalized.append(value)
    if len(normalized) > 200:
        raise ValueError('Salve até 200 CEPs por seleção.')
    existing = {row.postal_code: {'postal_code': row.postal_code, 'uf': row.uf, 'city': row.city}
                for row in db.query(LeadTimeDestination)}
    missing = [cep for cep in normalized if cep not in existing]
    def resolve_selected(cep):
        try:
            return resolve_cep(cep)
        except PostalCodeNotFound:
            if not skip_unknown:
                raise
            return None

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            resolved = dict(zip(missing, pool.map(resolve_selected, missing)))
    except requests.RequestException as exc:
        raise ValueError('Não foi possível validar os CEPs. Tente novamente; a seleção anterior foi mantida.') from exc
    unknown = [cep for cep, address in resolved.items() if address is None]
    valid = [cep for cep in normalized if cep not in unknown]
    if unknown and not valid:
        raise ValueError('Nenhum dos CEPs informados foi localizado.')
    db.query(LeadTimeDestination).delete(synchronize_session=False)
    for index, cep in enumerate(valid):
        db.add(LeadTimeDestination(**(existing.get(cep) or resolved[cep]), position=index))
    db.commit()
    result = {'custom': bool(valid), 'destinations': destinations(db)}
    if unknown:
        result['unknown_postal_codes'] = unknown
    return result
