"""Current Mercado Livre page data, scoped to one selected seller and destination."""
from datetime import datetime
from urllib.parse import urljoin,urlsplit,parse_qs,urlencode,urlunsplit
import re
import json
import chompjs
from bs4 import BeautifulSoup
from lxml import html as html_parser
from services.structured_products import number,shipping_sort_key
from services.product_resolver import resolve_candidate,normalize_text,MatchResult,_identifier_in_text,appliance_variant
from urllib.parse import quote
import time
from threading import Condition,RLock
from cachetools import TTLCache,cached

_account_snapshot_cache=TTLCache(maxsize=1,ttl=60)
_account_snapshot_condition=Condition(RLock())
_fast_account_preferred_until=0.0


def voltage_variant_url(html, url, identity):
    variant = appliance_variant(identity.sku)
    if not variant:
        return None
    suffix = variant.group(2)
    voltages = {'ANA': {'110v', '127v'}, 'BNA': {'220v'}, 'XNA': {'bivolt'}}[suffix]
    soup = BeautifulSoup(html, 'lxml')
    for anchor in soup.select('.ui-pdp-outside_variations a[href]'):
        label = normalize_text(anchor.get_text(' ', strip=True)).replace(' ', '')
        if label not in voltages:
            continue
        parts = urlsplit(urljoin(url, anchor['href']))
        if parts.scheme != 'https' or parts.hostname != 'www.mercadolivre.com.br' or not re.search(r'/p/MLB\d+$', parts.path):
            continue
        params = parse_qs(parts.query)
        for stale in ('wid', 'pdp_filters'):
            params.pop(stale, None)
        selected = urlunsplit(parts._replace(query=urlencode(params, doseq=True)))
        if selected != url:
            return selected
    return None


def initial_state(html):
    if not isinstance(html,str) or len(html)>8_000_000:return {}
    script=re.search(r'<script\b[^>]*\bid=["\x27]__NORDIC_RENDERING_CTX__["\x27][^>]*>(.*?)</script\s*>',html,re.S|re.I)
    if not script:return {}
    try:
        raw=script.group(1).split('=',1)[1].lstrip()
        try:payload=json.JSONDecoder().raw_decode(raw)[0]
        except ValueError:payload=chompjs.parse_js_object(raw)
        return payload.get('appProps',{}).get('pageProps',{}).get('initialState',{})
    except (ValueError,TypeError,IndexError):return {}


def selected_item_id(url):
    parts=urlsplit(url);query=parse_qs(parts.query)
    filters=(query.get('pdp_filters') or [''])[0]
    value=(query.get('wid') or query.get('item_id') or [filters or ('' if re.search(r'/(?:p|up)/',parts.path) else parts.path)])[0]
    match=re.search(r'MLB-?(\d+)',value,re.I)
    return 'MLB'+match.group(1) if match else None


def parse_primary(html,url,identity):
    state=initial_state(html);components=state.get('components') or {}
    if not isinstance(components,dict):return None
    event=(components.get('track') or state.get('track') or {}).get('melidata_event',{}).get('event_data',{})
    item_id=event.get('item_id');seller=event.get('seller_name')
    if not item_id or not seller:return None
    expected=selected_item_id(url)
    if expected and expected!=item_id:return {'status':'INVALID_MATCH','reason':'A página selecionou outro anúncio/lojista.'}
    parts=urlsplit(url);params=parse_qs(parts.query);params.update(wid=[item_id],pdp_filters=['item_id:'+item_id]);url=urlunsplit(parts._replace(query=urlencode(params,doseq=True)))
    tree=html_parser.fromstring(html);headings=tree.xpath('//h1')
    if not headings:return None
    attributes={}
    for row in tree.xpath('//tr[th and td]'):
        key,value=row.find('th'),row.find('td')
        attributes[' '.join(key.itertext()).strip().lower()]=' '.join(value.itertext()).strip()
    candidate=dict(name=' '.join(' '.join(headings[0].itertext()).split()),url=url,ean=attributes.get('código universal de produto'))
    if attributes.get('modelo') and _identifier_in_text(identity.model or identity.sku,attributes['modelo']):candidate['model']=attributes['modelo']
    if appliance_variant(identity.sku):
        candidate['model'] = attributes.get('modelo')
        selected_voltage = next(
            (value.split(':', 1)[1].strip() for value in (
                ' '.join(node.itertext()).strip() for node in
                tree.xpath('//*[contains(@class,"ui-pdp-outside_variations__title")]')
            ) if ':' in value and normalize_text(value.split(':', 1)[0]) in {'voltagem', 'tensao'}),
            None,
        )
        if normalize_text(selected_voltage) == 'escolha':
            return {'status': 'INVALID_MATCH', 'reason': 'A tensão do anúncio ainda precisa ser selecionada.'}
        candidate['voltage'] = selected_voltage or attributes.get('voltagem') or attributes.get('tensão')
    match=resolve_candidate(identity,candidate)
    verified_alias=False
    # The supplied "MAGISTER 4B INOX PTO" is manufacturer code 300001610.
    # Some stores publish only its marketing name: require all distinguishing specs.
    if identity.sku=='300001610' and match.reason=='Nenhum identificador forte confirmou o produto':
        title=normalize_text(candidate['name']);spec={normalize_text(k):normalize_text(v) for k,v in attributes.items()}
        verified_alias=(spec.get('marca')=='dako' and spec.get('modelo')=='magister' and spec.get('cor')=='preto'
            and spec.get('quantidade de bocas')=='4' and 'inox' in spec.get('acabamento da mesa','')
            and spec.get('com grill no forno')=='nao' and not re.search(r'\b(?:style|grill|vidro)\b',title))
        if verified_alias:match=MatchResult(True,.95,'brand_model_specs','Dako Magister: quatro bocas, preto, mesa inox e sem grill; referência do fabricante 300001610.')
    if not match.accepted:return {'status':'INVALID_MATCH','reason':match.reason}
    unavailable=event.get('has_stock') is False or (components.get('no_stock_status') or {}).get('state')=='VISIBLE'
    primary=(components.get('price') or {}).get('price') or {}
    amount=number(primary.get('value'))
    if amount is None and not unavailable:return None
    images=tree.xpath('//meta[@property="og:image"]')
    result=dict(**candidate,sku=identity.sku,item_id=item_id,source='Mercado Livre',store=seller,seller=seller,
        brand=attributes.get('marca') or identity.brand,category=identity.name or identity.sku,image_url=images[0].get('content') if images else None,
        price=None if unavailable else amount,cash_price=None if unavailable else amount,availability='out_of_stock' if unavailable else 'in_stock',
        match_confidence=match.confidence,match_method=match.method,captured_at=datetime.utcnow().isoformat(),discovery_source='mercado_livre_nordic')
    if event.get('logistic_type')=='fulfillment':result.update(fulfillment_type='2P',fulfilled_by='Mercado Livre',fulfillment_evidence='Modalidade de logística fulfillment publicada na página do anúncio.')
    if verified_alias:result.update(model=identity.model,identity_evidence=match.reason)
    return {'status':'OK','item':result,'items':[result],'match':match}


def calculator_url(item_id):
    if not re.fullmatch(r'MLB\d+',str(item_id)):raise ValueError('Anúncio Mercado Livre inválido.')
    return 'https://www.mercadolivre.com.br/gz/shipping-calculator?'+urlencode(dict(noIndex='true',item_id=item_id,new_version='true',modal='true',informative='true',page_context='vpp',location='true',quantity=1,can_go_cart_checkout='true'))


def parse_calculator(html,item_id,postal_code):
    state=initial_state(html)
    if re.sub(r'\D','',str(state.get('destinationValue') or ''))!=postal_code:
        raise ValueError('A loja não confirmou o CEP solicitado; a cotação não foi aproveitada.')
    event=state.get('track',{}).get('melidata_event',{}).get('event_data',{})
    if event.get('item_id')!=item_id:raise ValueError('A cotação corresponde a outro anúncio.')
    options=[]
    for row in event.get('shipping_promises') or []:
        if row.get('type')!='address':continue
        cost=number(row.get('cost'),allow_zero=True)
        if cost is None:continue
        lower,upper=row.get('min_days'),row.get('max_days')
        if any(v is not None and (isinstance(v,bool) or not isinstance(v,int) or v<0) for v in [lower,upper]):continue
        options.append(dict(shipping_cost=cost,shipping_method=row.get('shipping_method_type') or 'Entrega',
            delivery_min_days=lower,delivery_max_days=upper,delivery_days_type='calendar' if upper is not None else None,
            destination_zip=postal_code,logistics_source='mercado_livre_public_calculator',logistics_captured_at=datetime.utcnow().isoformat()))
    return min(options,key=lambda row:shipping_sort_key(row['shipping_cost'],row['delivery_max_days']),default={})


def quote_offer(item,postal_code,timeout=12):
    """Read a destination-specific public calculator, without altering account addresses."""
    from curl_cffi.requests import Session
    from services.structured_products import LOGISTICS_FIELDS
    host=urlsplit(item.get('url') or '').hostname or ''
    if host!='mercadolivre.com.br' and not host.endswith('.mercadolivre.com.br'):return None
    item_id=selected_item_id(item['url']) or item.get('item_id')
    if not item_id:return None
    if not re.fullmatch(r'\d{8}',postal_code):raise ValueError('CEP inválido.')
    from services.marketplace_intervention import suspension as pending,enabled,require_access,defer_access
    from services.mercado_livre_browser import page_status
    clean={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
    access=pending('mercado_livre')
    if access:return {**clean,'_logistics_error':access['reason'],'is_cached':True}
    if enabled('mercado_livre') and time.monotonic()<_fast_account_preferred_until:
        recovered=_fast_account_calculator(item_id,postal_code,timeout)
        if recovered:
            html,final_url,code=recovered
            if page_status(html,final_url,code)[0]=='OK':
                details=parse_calculator(html,item_id,postal_code)
                clean.update(details)
                if not details:clean['_logistics_error']='O anúncio não ofereceu serviço de entrega para este CEP.'
                return clean
    from services.extraction_resources import pooled_curl_session, request_slot
    with request_slot('mercado_livre'), pooled_curl_session(Session) as session:
        session.cookies.set('cp',quote(postal_code+'|'+str(int(time.time()*1000)),safe=''),domain='.mercadolivre.com.br',path='/')
        response=session.get(calculator_url(item_id),timeout=(5,timeout))
    status,reason=page_status(response.text,str(response.url),response.status_code)
    html=response.text
    if status in {'BLOCKED','LOGIN_REQUIRED','JS_REQUIRED'} and enabled('mercado_livre'):
        recovered=_account_calculator(item_id,postal_code,timeout)
        if recovered is None:
            defer_access('mercado_livre',item.get('sku'),'A sessão está ocupada ou o transporte da cotação falhou temporariamente.')
            return {**clean,'_logistics_error':'A cotação será tentada novamente automaticamente.','is_cached':True}
        if recovered:
            html,final_url,code=recovered
            status,reason=page_status(html,final_url,code)
        if status in {'BLOCKED','LOGIN_REQUIRED','JS_REQUIRED'}:
            require_access('mercado_livre',item.get('sku'),reason,item['url'])
            return {**clean,'_logistics_error':reason,'is_cached':True}
    if status!='OK':return {**clean,'_logistics_error':reason or status,'is_cached':True}
    details=parse_calculator(html,item_id,postal_code)
    clean.update(details)
    if not details:clean['_logistics_error']='O anúncio não ofereceu serviço de entrega para este CEP.'
    return clean


def _account_calculator(item_id,postal_code,timeout):
    """Retry using the enabled account without changing its saved addresses."""
    fast=_fast_account_calculator(item_id,postal_code,timeout)
    if fast is not None:return fast
    from services.marketplace_session import configuration,ensure_account_browser,account_context
    from patchright.sync_api import sync_playwright
    _,slot,_,_=configuration('mercado_livre')
    if not ensure_account_browser('mercado_livre') or not slot.acquire(timeout=2):return None
    try:
        with sync_playwright() as runtime:
            with account_context(runtime,'mercado_livre') as context:
                cookies=[cookie for cookie in context.cookies('https://www.mercadolivre.com.br/') if cookie['name']!='cp']
                cookie_header='; '.join([*(cookie['name']+'='+cookie['value'] for cookie in cookies),'cp='+quote(postal_code+'|'+str(int(time.time()*1000)),safe='')])
                response=context.request.get(calculator_url(item_id),headers={'Cookie':cookie_header},timeout=timeout*1000)
                return response.text(),response.url,response.status
    except Exception:return None
    finally:slot.release()


@cached(_account_snapshot_cache,condition=_account_snapshot_condition)
def _account_cookie_snapshot(validation_version):
    """Short-lived memory only; browser objects never cross thread boundaries."""
    from services.marketplace_session import configuration,ensure_account_browser,account_context
    from patchright.sync_api import sync_playwright
    _,slot,_,_=configuration('mercado_livre')
    if not ensure_account_browser('mercado_livre') or not slot.acquire(timeout=2):
        raise RuntimeError('Sessão temporariamente ocupada.')
    try:
        with sync_playwright() as runtime:
            with account_context(runtime,'mercado_livre') as context:
                cookies=[cookie for cookie in context.cookies('https://www.mercadolivre.com.br/') if cookie['name']!='cp']
                agent=context.pages[0].evaluate('navigator.userAgent') if context.pages else 'Mozilla/5.0'
                return '; '.join(cookie['name']+'='+cookie['value'] for cookie in cookies),agent
    finally:slot.release()


def _fast_account_calculator(item_id,postal_code,timeout):
    global _fast_account_preferred_until
    from services.marketplace_intervention import enabled
    if not enabled('mercado_livre'):return None
    try:
        from services.marketplace_session import metadata_file
        from services.extraction_resources import request_slot,pooled_curl_session
        from services.mercado_livre_browser import page_status
        from curl_cffi.requests import Session
        file=metadata_file('mercado_livre')
        version=file.stat().st_mtime_ns if file.exists() else 0
        cookies,agent=_account_cookie_snapshot(version)
        cookie_header=cookies+'; cp='+quote(postal_code+'|'+str(int(time.time()*1000)),safe='')
        with request_slot('mercado_livre'),pooled_curl_session(Session) as session:
            response=session.get(calculator_url(item_id),headers={'Cookie':cookie_header,'User-Agent':agent},timeout=(5,timeout))
        if page_status(response.text,str(response.url),response.status_code)[0]!='OK':return None
        parse_calculator(response.text,item_id,postal_code)  # Require the real item and CEP echo.
        _fast_account_preferred_until=time.monotonic()+600
        return response.text,str(response.url),response.status_code
    except Exception:return None  # Preserve the existing browser-request fallback.
