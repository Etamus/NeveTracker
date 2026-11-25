"""Store-scoped seller selection shared by collection, recovery and exports."""
import re
from services.product_resolver import normalize_text
from services.store_registry import find_target_store


def seller_key(store, value):
    text = normalize_text(value)
    if store == 'magalu':
        text = re.sub(r'\s+(?:magalu|magazine luiza)$', '', text)
    return text


def seller_matches(selection, item, store=None, allow_unknown=False):
    if not selection:
        return True
    if isinstance(item, dict):
        target = find_target_store(item.get('source'), item.get('url'))
        store = store or (target['key'] if target else None)
        seller = None if item.get('_seller_unknown') else item.get('seller') or item.get('store')
    else:
        target = find_target_store(getattr(item, 'source', None), getattr(item, 'url', None))
        store = store or (target['key'] if target else getattr(item, 'store', None))
        seller = getattr(item, 'seller', None)
    requested = selection.get(store, [])
    if not requested:
        return True
    normalized=seller_key(store,seller)
    if not normalized or normalized in {'nao informado','nao identificado','desconhecido','unknown','n a'}:
        return allow_unknown
    return normalized in {seller_key(store, name) for name in requested}
