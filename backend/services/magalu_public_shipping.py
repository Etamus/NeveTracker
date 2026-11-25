"""Public PDP data and the same delivery estimate requested by Magalu's UI."""
import json
from datetime import datetime
from urllib.parse import urlsplit, parse_qs
from bs4 import BeautifulSoup
from curl_cffi.requests import Session
from services.extraction_resources import pooled_curl_session, request_slot
from services.structured_products import shipping_sort_key, LOGISTICS_FIELDS
from services.product_resolver import appliance_variant

QUERY = '''query shippingQuery($shippingRequest: ShippingRequest!) {
  shipping(shippingRequest:$shippingRequest) { status
    ... on EstimateResponse { deliveries { id
      modalities { id name type cost { customer }
        shippingTime { unit value { min max } } }
      status { code message } } shippingAddress { zipCode } }
    ... on EstimateErrorResponse { error message }
  }
}'''


def product_page(url, timeout=12):
    host=urlsplit(url).hostname
    if host not in {'www.magazineluiza.com.br','www.magazinevoce.com.br'}:
        raise ValueError('URL fora da loja Magalu.')
    with request_slot('magalu'), pooled_curl_session(Session) as session:
        response=session.get(url,timeout=(5,timeout));response.raise_for_status()
    node=BeautifulSoup(response.text,'html.parser').select_one('script#__NEXT_DATA__')
    if not node:raise ValueError('A página do Magalu não liberou os dados do produto.')
    page=json.loads(node.get_text())['props']['pageProps']
    product=page['data']['product']
    expected=(parse_qs(urlsplit(url).query).get('seller_id') or [None])[0]
    if expected and product.get('seller',{}).get('id')!=expected:
        raise ValueError('A loja retornou outro lojista.')
    return product,page.get('structure') or {}


def shipping_request(product, structure, postal_code):
    seller=product['seller'];price=product['price'];route=structure.get('route') or {}
    return {'metadata':{'categoryId':(product.get('category') or {}).get('id',''),
        'clientId':'','organizationId':'magazine_luiza','pageName':'product',
        'partnerId':str(route.get('partner_id') or '0'),
        'salesChannelId':(structure.get('config') or {}).get('salesChannelId') or '1',
        'sellerId':seller['id'],'subcategoryId':(product.get('subcategory') or {}).get('id','')},
        'product':{'currency':price.get('currency') or 'BRL',
            'dimensions':{key:value for key,value in (product.get('dimensions') or {}).items() if key!='__typename'},
            'id':product.get('variationId') if seller.get('category','1p')=='1p' else seller['sku'],
            'price':float(price.get('fullPrice') or price['price']), 'quantity':1,'type':product.get('type') or 'product'},
        'zipcode':postal_code}


def simulate(product, structure, postal_code, timeout=12):
    with request_slot('magalu'), pooled_curl_session(Session) as session:
        response=session.post('https://federation.magazineluiza.com.br/graphql',
            json={'operationName':'shippingQuery','query':QUERY,'variables':{'shippingRequest':shipping_request(product,structure,postal_code)}},
            headers={'Origin':'https://www.magazinevoce.com.br','Referer':'https://www.magazinevoce.com.br/'},timeout=(5,timeout))
        response.raise_for_status();raw=response.json()
    return parse_estimate(raw,postal_code),raw


def shipping_failure(raw):
    result = (raw.get('data') or {}).get('shipping') or {}
    reason = result.get('message')
    if not reason:
        reason = next((status['message'] for delivery in result.get('deliveries') or []
                       if (status := delivery.get('status') or {}).get('message')), None)
    return reason or result.get('error')


def parse_estimate(raw,postal_code):
    result=(raw.get('data') or {}).get('shipping') or {}
    returned=(result.get('shippingAddress') or {}).get('zipCode')
    if returned and returned.replace('-','')!=postal_code:raise ValueError('Cotação retornada para outro CEP.')
    options=[]
    for delivery in result.get('deliveries') or []:
        for option in delivery.get('modalities') or []:
            if option.get('type') not in {'conventional','express','scheduled','delivery'}:continue
            cost=(option.get('cost') or {}).get('customer');estimate=option.get('shippingTime') or {};days=estimate.get('value') or {}
            if not isinstance(cost,(int,float)) or cost<0:continue
            unit=estimate.get('unit');kind='business' if unit in {'working_days','business_days'} else 'calendar' if unit in {'days','calendar_days'} else None
            options.append(dict(shipping_cost=cost,shipping_method=option.get('name'),delivery_min_days=days.get('min') if kind else None,
                delivery_max_days=days.get('max') if kind else None,delivery_days_type=kind,destination_zip=postal_code,
                logistics_source='magalu_public_shipping',logistics_captured_at=datetime.utcnow().isoformat()))
    return min(options,key=lambda option:shipping_sort_key(option['shipping_cost'],option['delivery_max_days']),default={})


def simulate_destinations(product,structure,postal_codes,timeout=12,batch_size=12):
    """Independent shipping fields in one GraphQL operation, not a combined cart.

    Each alias has its own product quantity=1 and exact CEP. Missing or failed
    aliases fall back individually, preserving every requested destination.
    """
    ceps=list(dict.fromkeys(postal_codes));output={}
    batch_size=max(1,min(12,int(batch_size)))
    selection=QUERY.split('shipping(shippingRequest:$shippingRequest)',1)[1].rsplit('}',1)[0].strip()
    for offset in range(0,len(ceps),batch_size):
        batch=ceps[offset:offset+batch_size]
        definitions=','.join(f'$r{index}:ShippingRequest!' for index in range(len(batch)))
        fields=' '.join(f'd{index}:shipping(shippingRequest:$r{index})'+selection for index in range(len(batch)))
        request={'operationName':'shippingDestinations','query':f'query shippingDestinations({definitions})'+'{'+fields+'}',
            'variables':{f'r{index}':shipping_request(product,structure,cep) for index,cep in enumerate(batch)}}
        try:
            with request_slot('magalu'),pooled_curl_session(Session) as session:
                response=session.post('https://federation.magazineluiza.com.br/graphql',json=request,
                    headers={'Origin':'https://www.magazinevoce.com.br','Referer':'https://www.magazinevoce.com.br/'},timeout=(5,timeout))
                response.raise_for_status();raw=response.json()
            values=raw.get('data') or {}
            failed={error.get('path',[None])[0] for error in raw.get('errors') or [] if error.get('path')}
        except Exception:
            values={};failed=set()
        for index,cep in enumerate(batch):
            alias=f'd{index}';value=values.get(alias)
            if isinstance(value,dict) and value and alias not in failed:
                scoped={'data':{'shipping':value}}
                try:
                    output[cep]=(parse_estimate(scoped,cep),scoped)
                    continue
                except (ValueError,TypeError,KeyError):pass
            try:
                output[cep]=simulate(product,structure,cep,timeout)
            except Exception as exc:
                # Persist successful aliases even when this individual retry
                # also fails. Only this incomplete CEP needs a later attempt.
                output[cep]=({}, {'data': {'shipping': {'message': str(exc)}}})
    return output


def quote_offer(item, postal_code, timeout=12, page_context=None):
    product,structure=page_context if page_context is not None else product_page(item['url'],timeout)
    _validate_product(item,product)
    if product.get('available') is False:
        return _quoted_item(item, product, postal_code, {}, {})
    details,raw=simulate(product,structure,postal_code,timeout)
    return _quoted_item(item,product,postal_code,details,raw)


def _product_voltage(product):
    return next(
        (attribute.get('current') or attribute.get('value') for attribute in product.get('attributes') or []
         if isinstance(attribute, dict) and attribute.get('type') == 'voltage'),
        None,
    )


def _published_fulfillment(seller):
    delivery = seller.get('deliveryId')
    if delivery and (delivery == 'magazineluiza' or delivery == seller.get('id')):
        return dict(fulfillment_type='2P' if delivery == 'magazineluiza' else '3P',
                    fulfilled_by=seller.get('deliveryDescription') or delivery,
                    fulfillment_evidence=f'Identificação de entrega publicada pela loja: {delivery}')
    return {}


def _validate_product(item,product):
    seller = str(item.get('seller') or item.get('store') or '')
    current_seller = (product.get('seller') or {}).get('id')
    if seller.lower().endswith('-magalu') and current_seller and seller[:-7].casefold() != str(current_seller).casefold():
        raise ValueError('A página confirmou outro lojista.')
    if item.get('sku'):
        from bs4 import BeautifulSoup
        from services.product_resolver import ProductIdentity, normalize_text, resolve_candidate

        title = product.get('title') or ''
        opening = BeautifulSoup(product.get('description') or '', 'html.parser').find('p')
        # Some sellers append the SKU to the title only in the opening paragraph.
        # Require that exact structure so compatibility text cannot identify the listing.
        if title and opening and normalize_text(opening.get_text(' ', strip=True)) == normalize_text(f"{title} {item['sku']}"):
            title = opening.get_text(' ', strip=True)
        candidate={'name':' '.join([title,str(product.get('reference') or '')]),'url':item['url']}
        if appliance_variant(item['sku']):
            candidate.update(model=product.get('reference'), voltage=_product_voltage(product))
        if not resolve_candidate(ProductIdentity(sku=item['sku'],model=item['sku']),candidate).accepted:
            raise ValueError('A página não confirmou o SKU solicitado.')


def _quoted_item(item,product,postal_code,details,raw):
    result={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
    available=product.get('available')
    price=product.get('price') or {}
    result.pop('_price_verified', None)
    if available is False:
        result.update(availability='out_of_stock',price=None,cash_price=None,installment_price=None,
                      installment_count=None,installment_value=None,payment_difference=None,_price_verified=True)
    elif price.get('bestPrice') or price.get('price'):
        current = float(price.get('bestPrice') or price['price'])
        result.update(price=current, cash_price=current, availability='in_stock', _price_verified=True)
        if result.get('installment_price') is None and price.get('price'):
            result['installment_price'] = float(price['price'])
        result.update(_published_fulfillment(product.get('seller') or {}))
    if result.get('_price_verified'):
        result['is_cached'] = False
    result.update(details)
    result['destination_zip']=postal_code
    result['_logistics_error'] = (None if details.get('delivery_max_days') is not None else
        shipping_failure(raw) or 'A loja não confirmou o prazo de entrega para este CEP.')
    return result


def quote_destinations(item,postal_codes,timeout=12,page_context=None,batch_size=12):
    product,structure=page_context if page_context is not None else product_page(item['url'],timeout)
    _validate_product(item,product)
    if product.get('available') is False:
        return {cep:_quoted_item(item,product,cep,{}, {}) for cep in postal_codes}
    return {cep:_quoted_item(item,product,cep,details,raw) for cep,(details,raw) in
        simulate_destinations(product,structure,postal_codes,timeout,batch_size).items()}


def collect_source(source, identity, postal_code, timeout=12, page_context=None):
    from services.product_resolver import resolve_candidate
    product,structure=page_context if page_context is not None else product_page(source.url,timeout)
    name=' '.join([product.get('title') or '',product.get('reference') or '']).strip()
    candidate = {'name': name, 'url': source.url, 'description': product.get('description')}
    variant = appliance_variant(identity.sku)
    if variant:
        candidate.update(model=product.get('reference'), voltage=_product_voltage(product))
    match=resolve_candidate(identity,candidate)
    if not match.accepted:return {'status':'INVALID_MATCH','reason':match.reason}
    seller=product['seller'];prices=product.get('price') or {};available=product.get('available')
    price=prices.get('bestPrice') or prices.get('price')
    if available is not False and not price:return {'status':'PARSER_ERROR','reason':'Oferta sem preço e sem confirmação de falta de estoque.'}
    item=dict(sku=identity.sku,name=name,brand=(product.get('brand') or {}).get('label') or identity.brand,
        category=(product.get('category') or {}).get('name') or 'Eletrodomésticos',image_url=product.get('image'),
        source='Magazine Luiza',store=seller['id']+'-Magalu',seller=seller['id']+'-Magalu',url=source.url,
        availability='out_of_stock' if available is False else 'in_stock',price=float(price) if price and available is not False else None,
        cash_price=float(price) if price and available is not False else None,
        installment_price=float(prices['price']) if prices.get('price') else None,
        discovery_source='magalu_public_product',is_cached=False)
    if variant:
        item.update(model=candidate['model'], voltage=candidate['voltage'])
    item.update(_published_fulfillment(seller))
    if postal_code and available is not False:
        try:
            details,raw=simulate(product,structure,postal_code,timeout)
            item.update(details)
            if details.get('delivery_max_days') is None:
                item['_logistics_error'] = shipping_failure(raw) or 'A loja não confirmou o prazo de entrega para este CEP.'
            if details:
                item['_logistics_attempted_zip']=postal_code
        except Exception as exc:
            item['_logistics_error']=str(exc)
        item['_http_logistics_attempted_zip']=postal_code
    return {'status':'OK','items':[item],'match':match}
