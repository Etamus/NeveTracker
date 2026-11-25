"""Discovery from search responses emitted by the signed-in Shopee page."""
from urllib.parse import urlsplit, parse_qs, urlencode
from services.shopee_public import money
from services.shopee_dom import search_cards


def parse_search(payload):
    if payload.get('error'):
        return [], False
    data = payload.get('data') or payload
    values = []
    for row in data.get('items') or []:
        basic = row.get('item_basic') or row.get('item_data') or row
        asset = row.get('item_card_displayed_asset') or {}
        shop, item = basic.get('shopid'), basic.get('itemid')
        name = basic.get('name') or asset.get('name')
        if not shop or not item or not name:
            continue
        display = basic.get('item_card_display_price') or {}
        price = basic.get('price')
        # Ordinary product promotion, without account-specific vouchers/Pix.
        if price is None:
            price = display.get('applied_product_promo_price')
        if price is None and basic.get('price_min') == basic.get('price_max'):
            price = basic.get('price_min')
        seller = row.get('shop_name') or row.get('shop_username') or f'Shopee Loja {shop}'
        value = dict(name=name, url=f'https://shopee.com.br/product/{shop}/{item}',
                     seller=seller, store=seller, source='Shopee', price=money(price),
                     shop_id=str(shop), item_id=str(item), discovery_source='authenticated_search_page')
        cash = display.get('price')
        if cash is not None and not any(display.get(key) for key in ('recommended_shop_voucher_info', 'recommended_platform_voucher_info', 'recommended_ads_voucher_info')):
            value['cash_price'] = money(cash)
        image = basic.get('image') or asset.get('image')
        if image:
            value['image_url'] = f'https://down-br.img.susercontent.com/file/{image}'
        if basic.get('stock') == 0:
            value.update(availability='out_of_stock', price=None)
        values.append(value)
    return values, bool(data.get('has_more') if 'has_more' in data else not data.get('nomore', True))


def search(url, timeout=15, diagnostics=None):
    from services.marketplace_access import limited, record_limit
    from services.shopee_page_transport import read_page
    from services.marketplace_intervention import enabled,pending
    if not enabled('shopee'):
        if diagnostics is not None:diagnostics.update(status='DISABLED',reason='Uso da conta Shopee desativado.')
        return [],None,False
    if not pending('shopee'):
        live_diagnostics={}
        live=_validated_tab_search(url,timeout,live_diagnostics)
        if live is not None:
            values,more,success=live
            if success:
                if diagnostics is not None:diagnostics.update(status='OK',reason=None)
                params=parse_qs(urlsplit(url).query);params['page']=[str(int((params.get('page') or ['0'])[0])+1)]
                return values,'https://shopee.com.br/search?'+urlencode(params,doseq=True) if more else None,True
            if live_diagnostics.get('status')=='BROWSER_BUSY':
                if diagnostics is not None:diagnostics.update(live_diagnostics)
                return [],None,False
    blocked = limited('shopee')
    if blocked:
        if diagnostics is not None:
            diagnostics.update(blocked)
        return [], None, False
    fetched = read_page(url, timeout)
    if fetched.get('status') != 'OK':
        if diagnostics is not None:
            diagnostics.update(status=fetched['status'], reason=fetched.get('reason'))
        return [], None, False
    keyword = (parse_qs(urlsplit(url).query).get('keyword') or [''])[0]
    items, has_more, successful = {}, False, False
    for response in fetched.get('payloads', []):
        if (parse_qs(urlsplit(response['url']).query).get('keyword') or [''])[0].casefold() != keyword.casefold():
            continue
        payload = response['data']
        if payload.get('error'):
            if payload['error'] == 90309999:
                record_limit('shopee')
            if diagnostics is not None:
                diagnostics.update(status='BLOCKED' if payload['error']==90309999 else 'PARSER_ERROR', reason='A Shopee não liberou os resultados nesta sessão.')
            continue
        successful = True
        values, more = parse_search(payload)
        has_more = has_more or more
        for value in values:
            items[(value['shop_id'], value['item_id'])] = value
    for value in search_cards(fetched.get('html', '')):
        # Visible cards may show a voucher/Pix final price. Keep their URLs
        # for PDP verification; only the structured response supplies prices.
        value['price'] = None
        value.pop('cash_price', None)
        items.setdefault((value['shop_id'], value['item_id']), value)
    next_url = None
    if has_more and items:
        params = parse_qs(urlsplit(url).query)
        params['page'] = [str(int((params.get('page') or ['0'])[0])+1)]
        next_url = 'https://shopee.com.br/search?'+urlencode(params, doseq=True)
    if diagnostics is not None and successful:
        diagnostics.update(status='OK', reason=None)
    elif diagnostics is not None and not items:
        diagnostics.setdefault('status', 'PARSER_ERROR')
        diagnostics.setdefault('reason', 'A página não liberou resultados verificáveis.')
    return list(items.values()), next_url, bool(items or successful)


def _validated_tab_search(url,timeout,diagnostics=None):
    import json
    import requests
    from services.marketplace_session import metadata_file,browser_endpoint,configuration
    from services.shopee_page_transport import CDP
    from services.shopee_live_search import search_tab
    try:target=json.loads(metadata_file('shopee').read_text(encoding='utf-8')).get('search_target_id')
    except (OSError,ValueError):return None
    if not target:return None
    endpoint=browser_endpoint('shopee')
    if not endpoint:return None
    _,slot,_,_=configuration('shopee')
    if not slot.acquire(timeout=min(2,timeout)):
        if diagnostics is not None:diagnostics.update(status='BROWSER_BUSY',reason='A aba está ocupada; a sessão não foi invalidada.')
        return [],False,False
    page=None
    try:
        tab=next((tab for tab in requests.get(endpoint+'/json/list',timeout=3).json() if tab['id']==target),None)
        if not tab:return None
        page=CDP(tab['webSocketDebuggerUrl']);state=search_tab(page,url,timeout)
        if state.get('blocked') or state.get('status'):
            if diagnostics is not None:diagnostics.update(status='BLOCKED' if state.get('blocked') else state['status'],reason=state.get('reason') or 'A loja recusou a consulta nesta aba.')
            return [],False,False
        values,_=parse_search({'items':state.get('rows',[]),'nomore':not state.get('more')})
        return values,bool(state.get('more')),bool(values or state.get('empty'))
    except Exception as exc:
        if diagnostics is not None:diagnostics.update(status='BROWSER_ERROR',reason=f'Falha de transporte da aba: {type(exc).__name__}.')
        return [],False,False
    finally:
        if page:page.close()
        slot.release()
