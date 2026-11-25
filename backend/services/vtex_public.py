"""Read storefront catalog and delivery simulations, without creating carts."""
import math
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from curl_cffi.requests import Session
from services.extraction_resources import pooled_curl_session, remaining_request_time, request_slot
from services.product_resolver import ProductIdentity, resolve_candidate, _identifier_in_text
from services.structured_products import number, shipping_sort_key

HOSTS = {'brastemp':'https://www.brastemp.com.br','consul':'https://www.consul.com.br',
         'electrolux':'https://loja.electrolux.com.br','midea':'https://www.midea.com.br'}
LABELS = {'brastemp':'Brastemp','consul':'Consul','electrolux':'Electrolux','midea':'Midea'}


def catalog(store, term, timeout=12, *, first_page_only=False):
    found=[];seen=set();start=0
    with request_slot(store), pooled_curl_session(Session) as session:
        while True:
            remaining = remaining_request_time()
            budget = (5, timeout) if remaining is None else min(timeout, remaining)
            response=session.get(HOSTS[store]+'/api/catalog_system/pub/products/search',params={'ft':term,'_from':start,'_to':start+49},timeout=budget)
            response.raise_for_status();rows=response.json()
            if not isinstance(rows,list):raise ValueError('Catálogo sem lista de produtos.')
            fresh=[row for row in rows if row.get('productId') not in seen]
            if not fresh:break
            found.extend(fresh);seen.update(row.get('productId') for row in fresh)
            if first_page_only or len(rows)<50:break
            start+=50
    if not found and store=='midea' and not first_page_only:
        family='geladeira' if term.upper().startswith('MD-RT') else 'cervejeira' if term.upper().startswith('MDRX') else 'maquina' if term.upper().startswith(('MA5','MAV')) else None
        if family:return catalog(store,family,timeout)
    return found


def catalog_offers(store, sku, products):
    identity=ProductIdentity(sku=sku,model=sku)
    offers=[]
    for product in products:
        name=product.get('productName') or ''
        candidate={'name':name,'model':product.get('productReference'),'url':product.get('link') or ''}
        if not resolve_candidate(identity,candidate).accepted:continue
        for variant in product.get('items') or []:
            refs=[str(ref.get('Value') or '') for ref in variant.get('referenceId') or []]
            if store=='midea' and not _identifier_in_text(sku,variant.get('name'),*refs):continue
            for seller in variant.get('sellers') or []:
                commercial=seller.get('commertialOffer') or {}
                amount=commercial.get('Price')
                price=float(amount) if isinstance(amount,(int,float)) and amount>0 else None
                available=bool(commercial.get('IsAvailable',commercial.get('AvailableQuantity',0)>0))
                payments=commercial.get('Installments') or []
                cash=[row.get('TotalValuePlusInterestRate') or row.get('Value') for row in payments if row.get('NumberOfInstallments')==1]
                cash=[float(value) for value in cash if isinstance(value,(int,float)) and value>0]
                installment=max((row for row in payments if row.get('NumberOfInstallments',0)>1),key=lambda row:row.get('NumberOfInstallments',0),default={})
                url=product.get('link') or HOSTS[store]+'/'+str(product.get('linkText') or '')+'/p'
                if urlsplit(url).hostname != urlsplit(HOSTS[store]).hostname:continue
                channel=(parse_qs(urlsplit(seller.get('addToCartLink') or '').query).get('sc') or ['1'])[0]
                url=url.split('?')[0]+'?'+urlencode({'idsku':variant['itemId'],'seller_id':seller['sellerId'],'sc':channel})
                offers.append(dict(sku=sku,name=' '.join(dict.fromkeys([name,str(variant.get('name') or '')])).strip(),
                    brand=product.get('brand'),category=(product.get('categories') or ['Eletrodomésticos'])[0].strip('/'),
                    image_url=next((img.get('imageUrl') for img in variant.get('images') or []),None),
                    source=LABELS[store],store=seller.get('sellerName') or LABELS[store],seller=seller.get('sellerName') or LABELS[store],
                    url=url,price=(min(cash) if cash else price) if available else None,cash_price=(min(cash) if cash else price) if available else None,
                    installment_price=installment.get('TotalValuePlusInterestRate'),installment_count=installment.get('NumberOfInstallments'),
                    installment_value=installment.get('Value'),availability='in_stock' if available else 'out_of_stock',
                    _vtex_sku=str(variant['itemId']),_vtex_seller=str(seller['sellerId']),_vtex_store=store,
                    _vtex_channel=channel,
                    fulfillment_type=None,fulfilled_by=None,fulfillment_evidence=None,
                    discovery_source='storefront_public_catalog'))
    return offers


def simulate(item, postal_code, timeout=12):
    store=item['_vtex_store']
    with request_slot(store), pooled_curl_session(Session) as session:
        response=session.post(HOSTS[store]+'/api/checkout/pub/orderForms/simulation',
            params={'sc':item.get('_vtex_channel') or '1'},
            json={'items':[{'id':item['_vtex_sku'],'quantity':1,'seller':item['_vtex_seller']}], 'postalCode':postal_code,'country':'BRA'},timeout=(5,timeout))
        response.raise_for_status();data=response.json()
    return simulation_shipping(data, postal_code, item['_vtex_sku'], item['_vtex_seller']), data


def simulation_shipping(data, postal_code, sku_id, seller_id, source='storefront_public_simulation'):
    """Read the SLA for the requested item only. A single estimate has no published lower bound."""
    returned_cep = data.get('postalCode')
    if returned_cep and re.sub(r'\D', '', str(returned_cep)) != postal_code:
        raise ValueError('A simulação retornou outro CEP.')
    items = data.get('items') or []
    if items:
        first = items[0]
        if (first.get('id') is not None and str(first['id']) != str(sku_id)
            or first.get('seller') is not None and str(first['seller']) != str(seller_id)):
            raise ValueError('A simulação retornou outra variante ou outro lojista.')
    options=[]
    for package in data.get('logisticsInfo') or []:
        if package.get('itemIndex')!=0:continue
        for sla in package.get('slas') or []:
            cost = number(sla.get('price'), allow_zero=True)
            if sla.get('deliveryChannel') != 'delivery' or cost is None:
                continue
            estimate = re.fullmatch(r'(\d+)(bd|d|h|m)', str(sla.get('shippingEstimate') or ''))
            days, unit = None, None
            if estimate:
                amount, suffix = int(estimate[1]), estimate[2]
                days = math.ceil(amount / {'h': 24, 'm': 1440}.get(suffix, 1))
                unit = 'business' if suffix == 'bd' else 'calendar'
            options.append(dict(shipping_cost=round(cost/100, 2), shipping_method=sla.get('name') or sla.get('id'),
                delivery_min_days=None, delivery_max_days=days, delivery_days_type=unit,
                destination_zip=postal_code, logistics_source=source))
    return min(options,key=lambda row:shipping_sort_key(row['shipping_cost'],row['delivery_max_days']),default={})


def _single_item_cash_price(data, row):
    price = number(row.get('sellingPrice'))
    if price is None:
        return None
    # Installment totals refer to the order. Use only a one-unit simulation
    # whose totalizers confirm no freight, fees or other items enter that sum.
    totals = data.get('totals') or []
    if (len(data.get('items') or []) == 1 and row.get('quantity') == 1
        and any(t.get('id') == 'Items' for t in totals)
        and all(t.get('id') in {'Items', 'Discounts'} or t.get('value') == 0 for t in totals)):
        for plan in (data.get('paymentData') or {}).get('installmentOptions') or []:
            for installment in plan.get('installments') or []:
                total = number(installment.get('total'))
                if installment.get('count') == 1 and total is not None:
                    price = min(price, total)
    return price / 100


def quote_offer(item, postal_code, timeout=12):
    from datetime import datetime
    from services.structured_products import LOGISTICS_FIELDS
    clean={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
    clean.pop('_price_verified', None)
    params=parse_qs(urlsplit(item['url']).query)
    store=next((key for key,host in HOSTS.items() if urlsplit(host).hostname==urlsplit(item['url']).hostname),None)
    sku_id=(params.get('idsku') or params.get('skuId') or params.get('skuid') or [None])[0]
    seller_id=(params.get('seller_id') or [None])[0]
    if not store or not sku_id or not seller_id:return None
    clean.update(_vtex_store=store,_vtex_sku=sku_id,_vtex_seller=seller_id,_vtex_channel=(params.get('sc') or ['1'])[0])
    details,data=simulate(clean,postal_code,timeout)
    row=(data.get('items') or [None])[0]
    messages='; '.join(str(message.get('text') or '') for message in data.get('messages') or [])
    if row and row.get('availability') in {'available','cannotBeDelivered'}:
        price = _single_item_cash_price(data, row)
        if price is not None:
            clean.update(price=price,cash_price=price,availability='in_stock',_price_verified=True)
    if row and row.get('availability') in {'withoutStock','inactive'}:
        clean.update(availability='out_of_stock',price=None,cash_price=None,installment_price=None,
                     installment_count=None,installment_value=None,payment_difference=None,_price_verified=True)
        details = {}
    clean.update(details,destination_zip=postal_code)
    if clean.get('_price_verified'):
        clean['is_cached']=False
    clean['_logistics_error']=messages or (None if details else 'A loja não ofereceu serviço de entrega para este SKU e CEP.')
    clean['_delivery_status']='quoted' if details else 'not_quoted'
    if details:clean['logistics_captured_at']=datetime.utcnow().isoformat()
    return clean


def index_catalog_offers(store, sku, products):
    indexed = {}
    for item in catalog_offers(store, sku, products):
        variants = indexed.setdefault((item['_vtex_sku'], item['_vtex_seller']), {})
        variants.setdefault(item['_vtex_channel'], item)
    return indexed


def published_catalog_listing(item, offer_index):
    """Resolve a channel-less PDP link only when the catalog publishes one matching offer."""
    url = urlsplit(item.get('url') or '')
    params = {key.lower(): value[0] for key, value in parse_qs(url.query).items()}
    if 'sc' in params or 'seller_id' not in params:
        return None
    variants = offer_index.get((params.get('skuid') or params.get('idsku'), params['seller_id']), {})
    if len(variants) != 1:
        return None
    published = next(iter(variants.values()))
    catalog_url = urlsplit(published['url'])
    if ((url.hostname, url.path.rstrip('/')) != (catalog_url.hostname, catalog_url.path.rstrip('/'))
        or item.get('price') != published.get('price')):
        return None
    from services.structured_products import offer_available
    if offer_available(item) != offer_available(published):
        return None
    return published


def collect_source(source, identity, postal_code, timeout=12, catalog_products=None, *, offer_index=None):
    """Refresh precisely the registered voltage/variant and seller, including stockouts."""
    params=parse_qs(urlsplit(source.url).query)
    sku_id=(params.get('idsku') or [None])[0]
    seller_id=(params.get('seller_id') or [None])[0]
    channel = (params.get('sc') or [None])[0]
    if offer_index is None:
        products = catalog(source.store, identity.sku, timeout) if catalog_products is None else catalog_products
        offer_index = index_catalog_offers(source.store, identity.sku, products)
    variants = offer_index.get((sku_id, seller_id), {})
    selected = variants.get(channel or '1')
    if selected is None and channel is None and len(variants) == 1:
        selected = next(iter(variants.values()))
    if selected is None:
        return {'status':'INVALID_MATCH','reason':'Variante, canal ou lojista não confirmado no catálogo atual.'}
    item = dict(selected)
    if postal_code and item['availability']=='in_stock':
        try:
            item=quote_offer(item,postal_code,timeout)
        except Exception as exc:
            # A valid live catalog listing still has a price and stock when
            # its delivery endpoint fails. Browser and CEP retries stay eligible.
            item.update(_http_logistics_attempted_zip=postal_code, _logistics_error=str(exc))
    return {'status':'OK','items':[item],'match':resolve_candidate(identity,item)}
