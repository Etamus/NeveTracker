from __future__ import annotations

import time
from types import SimpleNamespace

from services.http_client import create_resilient_session
from services.shopee_public import MOBILE_HEADERS, item_ids, money, parse_payload, parse_public_html

API_ROOT = "https://shopee.com.br/api/v4"
_price = money


def collect_shopee(source, identity, postal_code=None, timeout=15):
    shop_id, item_id = item_ids(source.url)
    if not shop_id or not item_id:
        return {"status": "INVALID_MATCH", "reason": "URL Shopee sem item_id/shop_id validos"}
    for field, actual in (("shop_id", shop_id), ("item_id", item_id)):
        stored = getattr(source, field, None)
        if stored and str(stored) != actual:
            return {"status": "INVALID_MATCH", "reason": "IDs da fonte divergem da URL Shopee"}
    checked_source = SimpleNamespace(url=source.url, shop_id=shop_id, item_id=item_id, seller=getattr(source, 'seller', None))
    deadline = time.monotonic() + max(1, timeout)
    headers = {"User-Agent": MOBILE_HEADERS['User-Agent'], "Referer": source.url, "x-api-source": "pc"}
    session = create_resilient_session(pool_size=2)
    result = {"status": "PARSER_ERROR", "reason": "Resposta sem dados do produto"}
    try:
        response = session.get(f"{API_ROOT}/item/get", params={"itemid": item_id, "shopid": shop_id},
                               headers=headers, timeout=(min(5, timeout), min(7, timeout)))
        if response.status_code == 200:
            payload = response.json()
            if not payload.get('error'):
                shop = {}
                # Shop metadata is optional; a failed name lookup must not
                # discard an otherwise valid item observation.
                try:
                    remaining = deadline - time.monotonic()
                    if remaining > 1:
                        reply = session.get(f"{API_ROOT}/shop/get_shop_base", params={"shopid": shop_id},
                                            headers=headers, timeout=(min(3, remaining), min(4, remaining)))
                        if reply.status_code == 200:
                            shop = reply.json().get('data') or {}
                except Exception:
                    pass
                result = parse_payload(payload, checked_source, identity, shop=shop)
                if result['status'] in {'OK', 'INVALID_MATCH'}:
                    return result
            else:
                result = parse_payload(payload, checked_source, identity)
        else:
            result = {'status': 'BLOCKED' if response.status_code in {401, 403, 429} else 'HTTP_ERROR',
                      'reason': f'Shopee HTTP {response.status_code}'}
            if response.status_code == 429:
                return result
        remaining = deadline - time.monotonic()
        if remaining > 1:
            page = session.get(source.url, headers=MOBILE_HEADERS, timeout=(min(4, remaining), remaining))
            if page.status_code == 200:
                public = parse_public_html(page.text, checked_source, identity)
                if public.get('candidate') or public['status'] in {'OK', 'INVALID_MATCH', 'BLOCKED'}:
                    return public
        return result
    except Exception as exc:
        return {'status': 'TIMEOUT' if 'timeout' in str(exc).lower() else 'PARSER_ERROR', 'reason': str(exc)[:300]}
    finally:
        session.close()
