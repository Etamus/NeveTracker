"""Shared persisted CEP selection; no state-level substitution of quotes."""
from concurrent.futures import ThreadPoolExecutor
import re

import requests
from models.product import LeadTimeDestination
from services.lead_time_destinations import CAPITALS, BY_UF


def destinations(db, custom=True):
    rows = db.query(LeadTimeDestination).order_by(LeadTimeDestination.position).all() if custom else []
    if not rows:
        return [{**dest, 'key': dest['uf']} for dest in CAPITALS]
    return [{**BY_UF[row.uf], 'key': row.postal_code, 'postal_code': row.postal_code,
             'capital': row.city, 'city': row.city, 'custom': True} for row in rows]


def resolve_cep(cep):
    known = next((dest for dest in CAPITALS if dest['postal_code'] == cep), None)
    if known:
        return {'postal_code': cep, 'uf': known['uf'], 'city': known['capital']}
    response = requests.get(f'https://viacep.com.br/ws/{cep}/json/', timeout=(4, 8))
    response.raise_for_status()
    data = response.json()
    if data.get('erro') or data.get('uf') not in BY_UF or not data.get('localidade'):
        raise ValueError(f'CEP {cep} não encontrado.')
    return {'postal_code': cep, 'uf': data['uf'], 'city': data['localidade']}


def save_destinations(db, ceps):
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
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            resolved = dict(zip(missing, pool.map(resolve_cep, missing)))
    except requests.RequestException as exc:
        raise ValueError('Não foi possível validar os CEPs. Tente novamente; a seleção anterior foi mantida.') from exc
    db.query(LeadTimeDestination).delete(synchronize_session=False)
    for index, cep in enumerate(normalized):
        db.add(LeadTimeDestination(**(existing.get(cep) or resolved[cep]), position=index))
    db.commit()
    return {'custom': bool(normalized), 'destinations': destinations(db)}
