"""Destination- and item-checked delivery data from Shopee's own PDP response."""
import math
import re
import json
from functools import lru_cache
from urllib.parse import urlencode
from datetime import datetime
from services.product_resolver import normalize_text
from services.structured_products import shipping_sort_key


@lru_cache(maxsize=256)
def _destination(postal_code):
    from services.delivery_destinations import resolve_cep
    from services.lead_time_destinations import BY_UF
    destination=resolve_cep(postal_code)
    return destination['city'],BY_UF[destination['uf']]['state']


def request_postal_payload(page, url, postal_code):
    """Read through the store's ordinary signed-in XHR, without editing addresses."""
    from services.shopee_public import item_ids
    shop,item=item_ids(url)
    if not shop or not item:return None
    city,state=_destination(postal_code)
    address='https://shopee.com.br/api/v4/pdp/get_pc?'+urlencode(dict(item_id=item,shop_id=shop,
        tz_offset_in_minutes=-180,buyer_zipcode=postal_code,city=city,state=state,district='',town='',
        vn_data_version='',incoming_pdp_page_source=0,incoming_pdp_page_scenario=0))
    expression="new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET',"+json.dumps(address)+",true);x.timeout=10000;x.onload=()=>resolve(x.responseText);x.onerror=()=>resolve('{}');x.ontimeout=x.onerror;x.send();})"
    response=page.call('Runtime.evaluate',expression=expression,awaitPromise=True,returnByValue=True)
    payload=json.loads(response.get('result',{}).get('value') or '{}')
    if payload.get('error')==90309999:
        from services.marketplace_access import record_limit
        record_limit('shopee')
    return {'url':address,'data':payload}


def quote_offer(item, postal_code, timeout=15):
    from services.shopee_public import collect_account_product
    from services.structured_products import LOGISTICS_FIELDS
    from services.product_resolver import ProductIdentity
    from types import SimpleNamespace
    clean={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
    identity=ProductIdentity(sku=item['sku'],model=item.get('model') or item['sku'],brand=item.get('brand'))
    result=collect_account_product(SimpleNamespace(url=item['url'],seller=item.get('seller') or item.get('store')),identity,postal_code,timeout)
    if result.get('status')!='OK':return {**clean,'is_cached':True,'_logistics_error':result.get('reason') or result.get('status')}
    return {**clean,**result['item'],'_price_verified':True,'is_cached':False}


def quote_destinations(item,postal_codes,timeout=15):
    """One owned PDP tab for all requested CEPs; validate each response echo."""
    from types import SimpleNamespace
    from services.marketplace_intervention import enabled,require_access,defer_access
    from services.shopee_page_transport import read_page
    from services.shopee_public import item_ids,parse_payload
    from services.product_resolver import ProductIdentity, appliance_variant
    from services.structured_products import LOGISTICS_FIELDS
    if not enabled('shopee'):return None
    clean={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
    source=SimpleNamespace(url=item['url'],seller=item.get('seller') or item.get('store'))
    identity=ProductIdentity(sku=item['sku'],model=item.get('model') or item['sku'],brand=item.get('brand'))
    shop,product=item_ids(item['url'])
    if not shop:return None
    try:
        fetched=read_page(item['url'],timeout,('/api/v4/pdp/get_pc','/api/v4/pdp/get_rw','/api/v4/item/get'),postal_codes=postal_codes)
    except Exception:return None  # Keep the single-destination collector as fallback.
    status=fetched.get('status')
    if status!='OK':
        if status in {'BLOCKED','LOGIN_REQUIRED'}:require_access('shopee',item['sku'],fetched.get('reason'),item['url'])
        elif status in {'BROWSER_ERROR','TIMEOUT','BROWSER_BUSY','LOGIN_PENDING','RETRY_LATER'}:defer_access('shopee',item['sku'],fetched.get('reason'))
        return {cep:{**clean,'destination_zip':cep,'is_cached':True,'_logistics_error':fetched.get('reason') or status} for cep in postal_codes}
    responses=fetched.get('payloads') or []
    refused=any((response.get('data') or {}).get('error')==90309999 for response in responses)
    needs_variant_confirmation=appliance_variant(identity.sku) is not None
    output={}
    for cep in postal_codes:
        options=[];verified=None
        for response in responses:
            payload=response.get('data') or {}
            if not isinstance(payload,dict):continue
            if response.get('requested_postal_code') not in {None,cep}:continue
            parsed=parse_payload(payload,source,identity,provenance='shopee_account_postal_batch')
            if parsed.get('status')=='OK':verified=parsed['item']
            details=parse_shipping(payload,shop,product,cep)
            if details and (not needs_variant_confirmation or parsed.get('status')=='OK'):
                options.append(details)
        details=min(options,key=lambda value:shipping_sort_key(value['shipping_cost'],value.get('delivery_max_days')),default={})
        result={**clean,**(verified or {}),**details,'destination_zip':cep,'is_cached':verified is None}
        if verified:result['_price_verified']=True
        if not details:result['_logistics_error']=(fetched.get('postal_errors') or {}).get(cep) or 'A Shopee não confirmou frete e prazo para este CEP.'
        output[cep]=result
    if refused:
        reason='A Shopee recusou a consulta do anúncio ou da entrega (90309999).'
        if any(row.get('_price_verified') for row in output.values()):
            # A verified product with a refused delivery request is not a logout.
            defer_access('shopee',item['sku'],reason,seconds=180)
        else:
            require_access('shopee',item['sku'],reason,item['url'])
    elif fetched.get('postal_retry_required'):
        defer_access('shopee',item['sku'],'A Shopee não respondeu à consulta dos CEPs; os destinos pendentes serão retomados.')
    return output


def _cost(value):
    if not isinstance(value,dict):return None
    amount=value.get('single_value')
    if isinstance(amount,bool) or not isinstance(amount,(int,float)) or not math.isfinite(amount) or amount<0:return None
    return round(amount/100000,2)


def parse_shipping(payload, shop_id, item_id, postal_code):
    if payload.get('error'):return {}
    data=payload.get('data') or payload;item=data.get('item') or {}
    actual=(str(item.get('shop_id') or item.get('shopid') or ''),str(item.get('item_id') or item.get('itemid') or ''))
    if actual!=(str(shop_id),str(item_id)):return {}
    if item.get('currency') not in {None,'BRL'}:return {}
    shipping=data.get('product_shipping') or {}
    if re.sub(r'\D','',str((shipping.get('delivery_address') or {}).get('zip_code') or ''))!=postal_code:return {}
    channels=list(shipping.get('ungrouped_channel_infos') or [])
    for group in shipping.get('grouped_channel_infos_by_service_type') or []:
        channels.extend(group.get('channel_infos') or [])
    if shipping.get('pre_selected_shipping_channel'):channels.append(shipping['pre_selected_shipping_channel'])
    options=[]
    for channel in channels:
        # A voucher reduction is conditional. Preserve the ordinary service
        # charge when Shopee publishes it beside the discounted charge.
        ordinary=_cost(channel.get('price_before_discount'))
        cost=ordinary if ordinary is not None else _cost(channel.get('price'))
        if cost is None:continue
        delivery=channel.get('channel_delivery_info') or {}
        lower,upper=delivery.get('estimated_delivery_time_min'),delivery.get('estimated_delivery_time_max')
        if any(value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<0) for value in [lower,upper]):continue
        method=channel.get('name') or 'Entrega Shopee'
        discounted=_cost(channel.get('price'))
        if ordinary is not None and discounted is not None and ordinary>discounted:method+=' (sem cupom)'
        option=dict(shipping_cost=cost,shipping_method=method,delivery_min_days=lower,delivery_max_days=upper,
            delivery_days_type='calendar' if upper is not None and delivery.get('display_mode')=='edt_by_date' else None,
            destination_zip=postal_code,logistics_source='shopee_authenticated_postal_quote',logistics_captured_at=datetime.utcnow().isoformat())
        if 'logistica do vendedor' in normalize_text(channel.get('name')):
            option.update(fulfillment_type='3P',fulfilled_by='Lojista',fulfillment_evidence='Canal da cotação identificado pela Shopee como logística do vendedor.')
        elif (channel.get('display_text') or {}).get('fulfilled_by_shopee') or re.search(r'\bshopee\s*xpress\b',normalize_text(channel.get('name'))):
            option.update(fulfillment_type='2P',fulfilled_by='Shopee',fulfillment_evidence='Canal da cotação identificado como entrega pela Shopee.')
        options.append(option)
    return min(options,key=lambda value:shipping_sort_key(value['shipping_cost'],value['delivery_max_days']),default={})
