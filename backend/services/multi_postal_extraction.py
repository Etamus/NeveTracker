"""Destination-specific quoting and persistence after one product discovery."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import json
from urllib.parse import urlsplit
import threading
from cachetools import TTLCache, cached
from models.product import LeadTimeQuote
from services.extraction_resources import check_cancelled, submit_in_context
from services.structured_products import LOGISTICS_FIELDS, offer_available
from services.store_registry import find_target_store


_postal_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='quote-cep')


def store_quote_snapshot(db, row, key, sku, item, cep, uf, observed_at=None):
    """Store the state and capture time of a single offer at its actual destination."""
    if row is None:
        row = LeadTimeQuote(
            quote_key=key, sku=sku, source=item['source'], seller=item['seller'],
            url=item['url'], uf=uf, postal_code=cep, item_json='{}',
        )
        db.add(row)
    for field in ('shipping_cost', 'shipping_method', 'delivery_min_days', 'delivery_max_days', 'delivery_days_type', 'logistics_source'):
        setattr(row, field, item.get(field))
    if item.get('_invalid_destination'):
        row.status = 'invalid_postal_code'
    elif not offer_available(item):
        row.status = 'out_of_stock'
    elif row.delivery_max_days is not None and row.shipping_cost is not None:
        row.status = 'quoted'
    else:
        row.status = 'failed' if item.get('is_cached') else 'not_quoted'
    row.error = item.get('_logistics_error')
    if not row.error and row.status not in {'quoted', 'out_of_stock'}:
        row.error = 'A loja não informou frete e prazo completos para este CEP.'
    row.item_json = json.dumps(item, ensure_ascii=False, default=str)
    row.updated_at = datetime.utcnow()
    row.captured_at = observed_at or row.updated_at
    return row


def collect_destinations(db, sku, offers, postal_codes, progress=None, refreshed_after=None, *, reuse_collected_quotes=False, persist=True):
    from services.offer_enrichment import enrich_offer
    from services.vtex_public import quote_offer
    from services.scraper_service import _coerce_datetime, _save_result, _offer_url_identity
    from services.lead_time_service import quote_key
    from services.delivery_destinations import PostalCodeNotFound, resolve_cep
    unique={}
    observed_quotes = {}
    postal_codes = list(dict.fromkeys(postal_codes))
    for item in offers:
        check_cancelled()
        store=find_target_store(item.get('source'),item.get('store'),item.get('url'))
        if not store or not item.get('url'):continue
        key=(store['key'],item.get('store'),_offer_url_identity(item['url']))
        normalized={**item,'sku':sku,'source':store['label'],'price':item.get('price',item.get('current_price')),'image_url':item.get('image_url'),'brand':item.get('brand'),'category':item.get('category')}
        if key not in unique or not item.get('is_cached'):unique[key]=normalized
        if reuse_collected_quotes and refreshed_after and item.get('is_cached') is False:
            cep = item.get('destination_zip')
            captured = _coerce_datetime(item.get('logistics_captured_at'))
            checked = _coerce_datetime(item.get('last_update'))
            if (cep in postal_codes and captured and checked and refreshed_after <= captured <= checked
                and offer_available(item) and item.get('shipping_cost') is not None
                and item.get('delivery_max_days') is not None):
                previous = observed_quotes.get((key, cep))
                if previous is None or captured > previous[0]:
                    observed_quotes[(key, cep)] = (captured, normalized)
    if not unique:
        return []
    def key_for(item,cep):return quote_key(sku,{**item,'seller':item.get('store') or item['source']},cep)
    requested = {id(item): {cep: key_for(item, cep) for cep in postal_codes} for item in unique.values()}
    saved_by_key={}
    if refreshed_after:
        requested_keys=[key for keys in requested.values() for key in keys.values()]
        from sqlalchemy import and_, or_
        for offset in range(0,len(requested_keys),400):
            check_cancelled()
            saved = db.query(
                LeadTimeQuote.quote_key, LeadTimeQuote.item_json, LeadTimeQuote.captured_at,
                LeadTimeQuote.status, LeadTimeQuote.shipping_cost, LeadTimeQuote.delivery_max_days,
            ).filter(
                LeadTimeQuote.quote_key.in_(requested_keys[offset:offset+400]),
                LeadTimeQuote.captured_at >= refreshed_after,
                or_(LeadTimeQuote.status.in_(('out_of_stock', 'invalid_postal_code')), and_(
                    LeadTimeQuote.status == 'quoted', LeadTimeQuote.shipping_cost.is_not(None),
                    LeadTimeQuote.delivery_max_days.is_not(None),
                )),
            )
            saved_by_key.update((row.quote_key,row) for row in saved)
        for (identity, cep), (captured, _) in observed_quotes.items():
            key = requested[id(unique[identity])][cep]
            checkpoint = saved_by_key.get(key)
            if checkpoint is not None and captured > checkpoint.captured_at:
                del saved_by_key[key]
        if len(saved_by_key) == len(requested_keys):
            return [json.loads(saved_by_key[key].item_json) for key in requested_keys]
    def reusable(row):
        return bool(refreshed_after and row and row.captured_at and row.captured_at>=refreshed_after and
                    (row.status in {'out_of_stock', 'invalid_postal_code'} or
                     row.status=='quoted' and row.shipping_cost is not None and row.delivery_max_days is not None))
    from services.extraction_resources import fair_map, resource_key, request_slot, ProgressMessage
    # Fresh page data is shared only inside this collection, never across runs.
    # Each destination still makes its own delivery simulation with the exact CEP.
    page_cache=TTLCache(maxsize=64,ttl=600)
    page_condition=threading.Condition(threading.RLock())
    @cached(page_cache,condition=page_condition)
    def magalu_context(url):
        from services.magalu_public_shipping import product_page
        return product_page(url)
    html_cache=TTLCache(maxsize=32,ttl=600)
    html_condition=threading.Condition(threading.RLock())
    @cached(html_cache,condition=html_condition)
    def html_context(url):
        from services.offer_enrichment import _session,_headers,_blocked_page_response
        with _session() as session:
            response=session.get(url,headers=_headers(url),timeout=(5,12))
            if response.status_code==403:
                from services.http_client import recover_public_html
                response=recover_public_html(response,url,12)
            if _blocked_page_response(response):
                return {'error':f'A página de cotação não foi liberada (HTTP {response.status_code}).'}
            if len(response.text)>4_000_000:raise ValueError('Página excede o tamanho de contexto permitido.')
            return {'offer_url':url,'page_url':response.url,'html':response.text}
    parsed_cache=TTLCache(maxsize=8,ttl=600)
    parsed_condition=threading.Condition(threading.RLock())
    @cached(parsed_cache,condition=parsed_condition)
    def parsed_context(url,sku,name,brand,source,seller,model,mpn,ean):
        context=html_context(url)
        if context.get('error'):return context
        from services.offer_enrichment import prepare_page_context
        return prepare_page_context(context,dict(sku=sku,name=name,brand=brand,source=source,
            seller=seller,store=seller,model=model,mpn=mpn,ean=ean))
    output=[]
    handled=set()

    def resolve_destination(cep):
        check_cancelled()
        try:
            return cep, resolve_cep(cep)
        except PostalCodeNotFound as exc:
            return cep, {'uf': '??', 'invalid': str(exc)}
        except Exception:
            return cep, {'uf': '??'}

    missing_ceps = [cep for cep in postal_codes if any(keys[cep] not in saved_by_key for keys in requested.values())]
    destination_tasks = [submit_in_context(_postal_workers, resolve_destination, cep) for cep in missing_ceps] if persist else []
    destinations = dict(task.result() for task in destination_tasks)
    def persist_quote(item,cep,requested_key,observed_at=None):
        seller=item.get('store') or item['source'];item['seller']=seller
        key=quote_key(sku,item,cep)
        row=db.query(LeadTimeQuote).filter_by(quote_key=key).populate_existing().first()
        if reusable(row) and (item.get('is_cached') or observed_at is not None and row.captured_at >= observed_at):
            output.append(json.loads(row.item_json));handled.add(requested_key)
            return
        if observed_at is None and not item.get('is_cached'):_save_result(item,db)
        store_quote_snapshot(db, row, key, sku, item, cep, destinations[cep]['uf'], observed_at)
        output.append(item);handled.add(requested_key)
        db.flush()

    def persist_batch(results, *, already_observed=False):
        if not results:
            return
        if not persist:
            output.extend(item for item, _, _ in results)
            handled.update(key for _, _, key in results)
            return
        # Serialize the recheck and write, but never hold SQLite's writer
        # while waiting for a store to respond. Checkpoint at most eight quotes.
        from sqlalchemy import text
        for offset in range(0, len(results), 8):
            check_cancelled()
            batch = results[offset:offset+8]
            if db.get_bind().dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            try:
                for item, cep, key in batch:
                    captured_at = _coerce_datetime(item.get('logistics_captured_at')) if already_observed else None
                    persist_quote(item, cep, key, observed_at=captured_at)
                db.commit()
            except Exception:
                db.rollback()
                raise

    if persist:
        db.commit()
    if observed_quotes:
        # The product phase already saved these exact offer/CEP observations.
        # Index them for Lead Time without requesting or recording them twice.
        fresh_quotes = []
        for (identity, cep), (_, item) in observed_quotes.items():
            quote_id = requested[id(unique[identity])][cep]
            if quote_id not in saved_by_key:
                fresh_quotes.append((item, cep, quote_id))
        persist_batch(fresh_quotes, already_observed=True)

    invalid_destinations = {cep: info['invalid'] for cep, info in destinations.items() if 'invalid' in info}
    if invalid_destinations:
        invalid_quotes = []
        for item in unique.values():
            for cep, reason in invalid_destinations.items():
                key = requested[id(item)][cep]
                if key in handled or key in saved_by_key:
                    continue
                clean = {field: value for field, value in item.items() if field not in LOGISTICS_FIELDS}
                quote = {**clean, 'destination_zip': cep, 'is_cached': True,
                         '_invalid_destination': True, '_logistics_error': reason}
                invalid_quotes.append((quote, cep, key))
        persist_batch(invalid_quotes)

    def quote(pair):
        item,cep=pair
        clean={key:value for key,value in item.items() if key not in LOGISTICS_FIELDS}
        # A stockout reported by a regional checkout applies only to that
        # destination. Other CEPs may have a different warehouse and stock.
        regional_stockout = (not offer_available(clean) and item.get('destination_zip')
            and item['destination_zip'] != cep and item['source'] in {'Brastemp', 'Consul', 'Electrolux', 'Midea'})
        try:
            if regional_stockout:
                clean.pop('availability', None)
                clean.pop('_price_verified', None)
                with request_slot(resource_key(item['url'])):
                    result = quote_offer(clean, cep)
                if not result or not result.get('_price_verified'):
                    raise ValueError('A loja não confirmou o estoque neste CEP.')
            elif not offer_available(clean):result=clean
            else:
                with request_slot(resource_key(item['url'])):
                    store_key=find_target_store(item.get('source'),item.get('store'),item['url'])['key']
                    if store_key=='shopee':
                        from services.shopee_shipping import quote_offer as quote_shopee
                        result=quote_shopee(clean,cep)
                    elif store_key=='magalu' and urlsplit(item['url']).hostname in {'www.magazineluiza.com.br','www.magazinevoce.com.br'}:
                        from services.magalu_public_shipping import quote_offer as quote_magalu
                        result=quote_magalu(clean,cep,page_context=magalu_context(item['url']))
                    else:
                        if store_key in {'friolar','gold_service','dufrio','mg_parts','comclick'}:
                            context=parsed_context(item['url'],item.get('sku'),item.get('name'),item.get('brand'),
                                item.get('source'),item.get('seller') or item.get('store'),item.get('model'),item.get('mpn'),item.get('ean'))
                            if context.get('error'):raise ValueError(context['error'])
                            clean['_page_context']=context
                        result=quote_offer(clean,cep) or enrich_offer(clean,cep,force_refresh=True)
            returned=result.get('destination_zip') or result.get('shipping_zip_code')
            if returned and returned!=cep:raise ValueError('A loja retornou cotação para outro CEP.')
            result.update(destination_zip=cep)
            result.pop('_page_context',None)
            if result.get('_price_verified'):result['is_cached']=False
            return result
        except Exception as exc:
            return {**{key:value for key,value in clean.items() if key!='_page_context'},'destination_zip':cep,'_logistics_error':str(exc),'is_cached':True}
    # Save all destinations for one Shopee listing before moving to the next.
    # One navigation/throttle interval replaces N repeated PDP openings, and
    # a restart retains the exact offer/CEP checkpoints already committed.
    for item in unique.values():
        check_cancelled()
        if find_target_store(item.get('source'),item.get('store'),item['url'])['key']!='shopee' or not offer_available(item):continue
        keys = requested[id(item)]
        missing=[cep for cep in postal_codes if keys[cep] not in handled and keys[cep] not in saved_by_key]
        if len(missing)<2:continue
        from services.shopee_shipping import quote_destinations
        if progress:progress(f'{sku}: Shopee, consultando {len(missing)} CEPs na mesma página do anúncio')
        try:batch=quote_destinations(item,missing)
        except Exception:batch=None
        if batch is None:continue
        results = []
        for cep in missing:
            quoted=batch.get(cep)
            if quoted is None:continue
            returned=quoted.get('destination_zip') or quoted.get('shipping_zip_code')
            if returned!=cep:
                quoted={**{key:value for key,value in item.items() if key not in LOGISTICS_FIELDS},'destination_zip':cep,'is_cached':True,'_logistics_error':'A loja retornou cotação para outro CEP.'}
            results.append((quoted,cep,keys[cep]))
        persist_batch(results)
    pending=[]
    for item in unique.values():
        for cep in postal_codes:
            key=requested[id(item)][cep]
            if key in handled:continue
            saved=saved_by_key.get(key)
            if reusable(saved):
                output.append(json.loads(saved.item_json))
            else:pending.append((item,cep))
    # Interleave hosts and destinations so one slow storefront cannot stall
    # all other quotes. ORM writes stay on this thread; save completed work
    # immediately instead of waiting for the first submitted HTTP request.
    grouped={}
    for pair in pending:
        grouped.setdefault(urlsplit(pair[0]['url']).hostname,[]).append(pair)
    pending=[values[index] for index in range(max(map(len,grouped.values()),default=0))
             for values in grouped.values() if index<len(values)]
    if progress:progress(f'{sku}: cotando {len(pending)} pares de oferta e CEP; {len(output)} já preservados')
    jobs=[];magalu_groups={}
    for pair in pending:
        item,cep=pair
        if resource_key(item['url'])=='magalu' and offer_available(item):
            key=(item['url'],item.get('store'))
            magalu_groups.setdefault(key,[]).append(pair)
        else:jobs.append([pair])
    magalu_batch_size = 12
    for pairs in magalu_groups.values():
        jobs.extend(pairs[offset:offset+magalu_batch_size] for offset in range(0,len(pairs),magalu_batch_size))
    def quote_job(pairs):
        check_cancelled()
        if len(pairs)>1:
            from services.magalu_public_shipping import quote_destinations
            item=pairs[0][0];ceps=[cep for _,cep in pairs]
            try:
                values=quote_destinations(item,ceps,page_context=magalu_context(item['url']),batch_size=magalu_batch_size)
                return [(item,cep,values[cep] if cep in values else quote((item,cep))) for cep in ceps]
            except Exception:pass  # Keep the single-destination recovery path.
        return [(item,cep,quote((item,cep))) for item,cep in pairs]
    checked=0
    for _, results in fair_map(quote_job,jobs,key=lambda pairs:resource_key(pairs[0][0]['url'])):
        completed = []
        for original,cep,result in results:
            returned=result.get('destination_zip') or result.get('shipping_zip_code')
            if returned!=cep:
                result={**{key:value for key,value in original.items() if key not in LOGISTICS_FIELDS},
                    'destination_zip':cep,'is_cached':True,'_logistics_error':'A loja retornou cotação para outro CEP.'}
            completed.append((result,cep,requested[id(original)][cep]))
        persist_batch(completed)
        checked += len(completed)
        if progress and completed:
            progress(ProgressMessage(f'{sku}: {checked}/{len(pending)} cotações verificadas; CEP {completed[-1][1]}',
                phase='quoting',done=checked,total=len(pending)))
    return output
