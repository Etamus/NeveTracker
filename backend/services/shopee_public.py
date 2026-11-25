"""Public Shopee item data. Missing/redacted values never become prices or stock.

URL identity follows the documented examples in paulodarosa/shopee-scraper;
embedded-state recovery follows the approach described by
perdanacarrer/shopee-mobile-scarper-api. No external runtime or paid API is used.
"""
from datetime import datetime
import json
import math
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from services.product_resolver import appliance_variant, normalize_text, resolve_candidate

MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'pt-BR,pt;q=0.9',
}


def item_ids(url):
    parsed = urlparse(str(url or ''))
    if parsed.scheme != 'https' or parsed.hostname not in {'shopee.com.br', 'www.shopee.com.br'}:
        return None, None
    path = unquote(parsed.path)
    match = re.search(r'(?:/product/(\d+)/(\d+)|-i\.(\d+)\.(\d+))/?$', path)
    return (match.group(1) or match.group(3), match.group(2) or match.group(4)) if match else (None, None)


def money(value):
    """Shopee's JSON money fields are integer BRL units scaled by 100,000."""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value) / 100000
        return round(amount, 5) if math.isfinite(amount) and amount > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _blocks(payload):
    """Bounded traversal of JSON objects, never eval page JavaScript."""
    stack, visited = [(payload, 0)], 0
    while stack and visited < 15000:
        value, depth = stack.pop(); visited += 1
        if depth > 24:
            continue
        if isinstance(value, dict):
            if isinstance(value.get('item'), dict) and (value['item'].get('name') or value['item'].get('title')):
                yield value, value['item']
            if (value.get('name') or value.get('title')) and (value.get('itemid') or value.get('item_id')):
                yield value, value
            stack.extend((child, depth + 1) for child in value.values() if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value if isinstance(child, (dict, list)))


def parse_payload(payload, source, identity, shop=None, provenance='shopee_internal_api'):
    if not isinstance(payload, dict):
        return {'status': 'PARSER_ERROR', 'reason': 'Resposta Shopee sem objeto JSON'}
    if payload.get('error'):
        denied=payload['error']==90309999
        return {'status':'BLOCKED' if denied else 'HTTP_ERROR','reason':f"Shopee recusou a coleta (erro {payload['error']})." if denied else f"Erro temporário na resposta Shopee ({payload['error']})."}
    url_shop, url_item = item_ids(source.url)
    shop_id = str(getattr(source, 'shop_id', None) or url_shop or '')
    item_id = str(getattr(source, 'item_id', None) or url_item or '')
    if not url_shop or (shop_id, item_id) != (url_shop, url_item):
        return {'status': 'INVALID_MATCH', 'reason': 'IDs da fonte divergem da URL Shopee'}
    data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
    candidates = list(_blocks(data))
    # Legacy item/get may omit IDs; only its direct data object can inherit
    # the request identity. Nested recommendations must always provide IDs.
    if not candidates and (data.get('name') or data.get('title')) and not (data.get('itemid') or data.get('item_id')):
        candidates = [(data, data)]
    candidates.sort(key=lambda pair: (bool(pair[0].get('removed_fields')), 'product_price' in pair[0]), reverse=True)
    for block, item in candidates:
        actual = (str(item.get('shopid') or item.get('shop_id') or shop_id), str(item.get('itemid') or item.get('item_id') or item_id))
        if actual != (shop_id, item_id):
            continue
        candidate = {'name': item.get('name') or item.get('title'), 'url': source.url,
                     'brand': item.get('brand', {}).get('name') if isinstance(item.get('brand'), dict) else item.get('brand'),
                     'model': next((attribute.get('value') for attribute in item.get('attributes') or [] if isinstance(attribute, dict) and str(attribute.get('name', '')).lower() in {'modelo', 'model'}), None),
                     'item_id': item_id, 'shop_id': shop_id}
        if appliance_variant(identity.sku):
            models = item.get('models') or block.get('models') or []
            labels = {normalize_text(model.get('name')) for model in models if isinstance(model, dict) and model.get('name')}
            if len(models) > 1 and (not labels or len(labels) > 1):
                return {'status': 'INVALID_MATCH', 'reason': 'O anúncio não identificou uma única variante selecionada.'}
            candidate['voltage'] = next(
                (attribute.get('value') for attribute in item.get('attributes') or []
                 if isinstance(attribute, dict) and normalize_text(attribute.get('name')) in {'voltagem', 'tensao', 'voltage'}),
                None,
            ) or ' '.join(labels)
        # The slug can retain an old SKU after a seller changes the listing.
        # Only the returned title/attributes can confirm the material itself.
        match = resolve_candidate(identity, {**candidate, 'url': f'https://shopee.com.br/product/{shop_id}/{item_id}'})
        if not match.accepted:
            return {'status': 'INVALID_MATCH', 'reason': match.reason, 'match': match}
        if item.get('currency') not in {None, 'BRL'}:
            return {'status': 'INVALID_MATCH', 'reason': 'Moeda do anúncio não é BRL', 'match': match}
        shop_data = shop or block.get('shop_detailed') or {}
        account = shop_data.get('account') or {}
        saved_seller = getattr(source, 'seller', None)
        if str(saved_seller or '').strip().lower() == 'shopee':
            saved_seller = None
        seller = shop_data.get('name') or account.get('username') or saved_seller or f'Shopee Loja {shop_id}'
        raw_price = item.get('price')
        product_price = block.get('product_price') or {}
        display_price = product_price.get('price')
        conditional_price = bool(product_price.get('has_final_price') or product_price.get('final_price_vouchers'))
        if isinstance(display_price, dict):
            raw_price = display_price.get('single_value')
            interval = display_price.get('range') or {}
            if raw_price is None and interval.get('min') == interval.get('max'):
                raw_price = interval.get('min')
        elif display_price is not None:
            raw_price = display_price
        if conditional_price:
            # The large displayed number may include a personal voucher and
            # Pix discount. Publish the ordinary price explicitly returned
            # by the same item, never the conditional final price.
            raw_price = item.get('price')
        if raw_price is None and item.get('price_min') == item.get('price_max'):
            raw_price = item.get('price_min')
        models = item.get('models') or []
        unavailable = item.get('stock') == 0 or item.get('is_unavailable') is True or str(item.get('item_status')).lower() in {'sold_out', 'out_of_stock'}
        if models and all(model.get('has_stock') is False for model in models):
            unavailable = True
        price = money(raw_price)
        if price is None and not unavailable:
            return {'status': 'BLOCKED' if block.get('removed_fields') else 'PARSER_ERROR',
                    'reason': 'A página pública identifica o produto, mas não libera seu preço. Nenhum preço ou estoque foi presumido.',
                    'candidate': {**candidate, 'seller': seller}, 'match': match}
        plan = next((value for value in item.get('installment_plans') or [] if isinstance(value, dict)), {})
        plan = plan or (product_price.get('installment_info') or {}).get('recommended_plan') or {}
        count = plan.get('installment_count') or plan.get('tenure') or plan.get('months')
        installment = money(plan.get('installment_amount') or plan.get('pay_per_month'))
        total = round(count * installment, 2) if isinstance(count, int) and count > 0 and installment else None
        result = {**candidate, 'sku': identity.sku, 'category': identity.sku, 'brand': candidate['brand'] or identity.brand or 'Não informada',
                  'store': seller, 'seller': seller, 'source': 'Shopee', 'price': price, 'cash_price': price,
                  'installment_count': count, 'installment_value': installment, 'installment_price': total,
                  'payment_difference': round(max(0, total-price), 2) if total and price is not None else None,
                  'availability': 'out_of_stock' if unavailable else 'in_stock' if (item.get('stock') is not None and item['stock'] > 0) or any(model.get('has_stock') is True for model in models) else None,
                  'image_url': f"https://down-br.img.susercontent.com/file/{item['image']}" if item.get('image') else None,
                  'match_confidence': match.confidence, 'match_method': match.method,
                  'captured_at': datetime.utcnow().isoformat(), 'discovery_source': provenance}
        # Shipping requires a destination-specific observation. Generic free
        # shipping labels, coupons and model estimated_days are not quotations.
        return {'status': 'OK', 'item': result, 'match': match}
    return {'status': 'PARSER_ERROR', 'reason': 'Resposta sem o par item/loja solicitado'}


def parse_public_html(html, source, identity):
    if not isinstance(html, str) or len(html) > 4_000_000:
        return {'status': 'PARSER_ERROR', 'reason': 'Página Shopee inválida ou acima do limite de leitura'}
    soup = BeautifulSoup(html, 'html.parser')
    last = None
    for script in soup.select('script[type="text/mfe-initial-data"], script#__NEXT_DATA__, script#__INITIAL_STATE__'):
        try:
            result = parse_payload(json.loads(script.get_text()), source, identity, provenance='shopee_public_html')
        except (TypeError, ValueError, RecursionError):
            continue
        if result['status'] in {'OK', 'INVALID_MATCH'}:
            return result
        if result.get('candidate'):
            last = result
    if last:
        return last
    text = soup.get_text(' ', strip=True)
    if re.search(r'login necess[aá]rio|fa[cç]a login para continuar|captcha|verifique sua identidade|verifica[cç][aã]o de seguran[cç]a|atividade incomum', text, re.I):
        return {'status': 'BLOCKED', 'reason': 'Shopee solicitou login/verificação; a coleta não foi confirmada.'}
    return {'status': 'PARSER_ERROR', 'reason': 'HTML público sem dados estruturados do anúncio'}


def collect_account_product(source, identity, postal_code=None, timeout=15):
    from services.marketplace_intervention import suspension as pending,require_access,defer_access
    access=pending('shopee')
    if access:return {'status':'ACCESS_REQUIRED','reason':access['reason']}
    result=collect_browser(source, identity, timeout, postal_code=postal_code)
    if result.get('status')=='OK' and (result.get('item') or {}).get('_access_required'):
        defer_access('shopee',identity.sku,(result.get('item') or {}).get('_logistics_error'),seconds=180)
    elif result.get('status') in {'BLOCKED','LOGIN_REQUIRED'} or (result.get('item') or {}).get('_access_required'):
        require_access('shopee',identity.sku,result.get('reason') or (result.get('item') or {}).get('_logistics_error'),source.url)
    elif result.get('status') in {'BROWSER_BUSY','BROWSER_ERROR','TIMEOUT','RETRY_LATER','LOGIN_PENDING'}:
        defer_access('shopee',identity.sku,result.get('reason'))
    return result


def collect_browser(source, identity, timeout=15, postal_code=None):
    """Observe the public page's own responses; never solve a login challenge."""
    from services.marketplace_session import ensure_account_browser
    if ensure_account_browser('shopee'):
        from services.shopee_page_transport import read_page
        from services.marketplace_access import limited, record_limit
        blocked = limited('shopee')
        if blocked:
            return blocked
        if not all(item_ids(source.url)):
            return {'status': 'INVALID_MATCH', 'reason': 'URL fora de um anúncio Shopee Brasil'}
        try:
            fetched = read_page(source.url, timeout, ('/api/v4/pdp/get_pc', '/api/v4/pdp/get_rw', '/api/v4/item/get'), postal_code=postal_code)
            if fetched.get('status') != 'OK':
                return {'status': fetched['status'], 'reason': fetched.get('reason')}
            access_refused=False
            for response in fetched.get('payloads', []):
                payload = response['data']
                if payload.get('error') == 90309999:
                    record_limit('shopee')
                if payload.get('error')==90309999:access_refused=True
                result = parse_payload(payload, source, identity, provenance='shopee_account_page')
                if result['status'] == 'OK':
                    if access_refused:result['item']['_access_required']=True
                    if postal_code:
                        from services.shopee_shipping import parse_shipping
                        shop,item=item_ids(source.url)
                        result['item'].update(parse_shipping(payload,shop,item,postal_code))
                        if result['item'].get('delivery_max_days') is None:
                            result['item']['_logistics_error']=fetched.get('postal_error') or 'A Shopee não confirmou entrega para este CEP.'
                    return result
            result=parse_public_html(fetched.get('html', ''), source, identity)
            if access_refused:
                if result.get('status')=='OK':
                    result['item']['_access_required']=True
                    result['item']['_logistics_error']='A Shopee recusou a consulta do anúncio ou da entrega (erro 90309999).'
                else:return {'status':'BLOCKED','reason':'A Shopee recusou a consulta deste anúncio (erro 90309999).'}
            return result
        except Exception as exc:
            return {'status': 'BROWSER_ERROR', 'reason': str(exc)[:250]}
    from urllib.parse import parse_qs
    from services.marketplace_session import configuration,account_context,session_status
    profile,BROWSER_SLOT,_,_=configuration('shopee')
    if session_status('shopee')['status'] in {'opening','waiting'}:
        return {'status':'LOGIN_PENDING','reason':'Conclua o login da Shopee na janela aberta.'}
    from services.browser_health import record_browser_result
    import time
    if not all(item_ids(source.url)):
        return {'status': 'INVALID_MATCH', 'reason': 'URL fora de um anúncio Shopee Brasil'}
    try:
        from patchright.sync_api import sync_playwright
        with BROWSER_SLOT, sync_playwright() as runtime:
            profile.mkdir(parents=True,exist_ok=True)
            manager=account_context(runtime,'shopee')
            context=manager.__enter__()
            try:
                page = context.new_page()
                results = []
                expected_shop, expected_item = item_ids(source.url)

                def observed(response):
                    parsed = urlparse(response.url)
                    if parsed.hostname != 'shopee.com.br' or not re.fullmatch(r'/api/v4/(?:pdp/get_(?:pc|rw)|item/get)', parsed.path):
                        return
                    params = parse_qs(parsed.query)
                    actual = ((params.get('shop_id') or params.get('shopid') or [''])[0],
                              (params.get('item_id') or params.get('itemid') or [''])[0])
                    if actual != (expected_shop, expected_item) or response.status != 200:
                        return
                    try:
                        results.append(parse_payload(response.json(), source, identity, provenance='shopee_public_browser'))
                    except Exception:
                        pass

                page.on('response', observed)
                deadline = time.monotonic() + timeout
                page.goto(source.url, wait_until='domcontentloaded', timeout=max(1000, timeout*1000))
                while time.monotonic() < deadline:
                    ready = next((result for result in results if result['status'] == 'OK'), None)
                    if ready:
                        record_browser_result(source.url, 'OK')
                        return ready
                    if '/verify/' in page.url or '/buyer/login' in page.url or any(result['status'] == 'BLOCKED' for result in results):
                        record_browser_result(source.url, 'BLOCKED')
                        return {'status': 'BLOCKED', 'reason': 'Shopee solicitou login/verificação. Sem sessão autenticada, preço e vendedor não foram confirmados.'}
                    page.wait_for_timeout(250)
                result = parse_public_html(page.content(), source, identity)
                record_browser_result(source.url, result['status'])
                return result
            finally:
                if not page.is_closed():page.close()
                manager.__exit__(None,None,None)
    except Exception as exc:
        return {'status': 'BROWSER_ERROR', 'reason': str(exc)[:250]}
