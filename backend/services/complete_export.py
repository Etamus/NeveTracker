"""One workbook containing the saved data behind all analytical views."""
from collections import defaultdict
from statistics import mean
import json
from datetime import datetime, time, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import exists
from models.product import ExtractionJob,LeadTimeQuote,OfferObservation,Product,PriceHistory,ProductSource
from services.export_workbook import ExportWorkbook,append,finish
from services.analytics_export import _excel_datetime
from services.analysis_workbooks import STATUS
from services.structured_products import offer_available
from services.seller_selection import seller_matches
from services.product_sources import HIDDEN_CURRENT_SOURCE_STATUSES


def logistics_for_export(db, offers, lower, upper):
    """Join the exact offer and CEP, including the reasons for failed quotes."""
    from services.lead_time_service import quote_key
    from services.structured_products import LOGISTICS_FIELDS
    keyed = defaultdict(list)
    for item in offers:
        postal_code = item.get('destination_zip')
        item['logistics_status'] = 'Sem CEP na coleta' if not postal_code else 'Cotação sem confirmação'
        item['logistics_note'] = 'A coleta registrou o preço sem um destino de entrega.' if not postal_code else None
        if not offer_available(item):
            item['logistics_status'] = 'Sem estoque'
            item['logistics_note'] = 'A oferta não estava disponível para compra.'
        elif item.get('shipping_cost') is not None and item.get('delivery_max_days') is not None:
            item['logistics_status'] = 'Cotação completa'
            item['logistics_note'] = 'Não se aplica'
            continue
        elif postal_code:
            item['logistics_note'] = 'Nenhum frete e prazo completos foram registrados para esta oferta e CEP.'
        if postal_code and item.get('sku') and item.get('source') and item.get('store') and item.get('url'):
            keyed[quote_key(item['sku'], {**item, 'seller': item['store']}, postal_code)].append(item)
    keys = list(keyed)
    for offset in range(0, len(keys), 400):
        query = db.query(LeadTimeQuote.quote_key, LeadTimeQuote.status, LeadTimeQuote.error,
                         LeadTimeQuote.captured_at, LeadTimeQuote.shipping_cost, LeadTimeQuote.shipping_method,
                         LeadTimeQuote.delivery_min_days, LeadTimeQuote.delivery_max_days, LeadTimeQuote.delivery_days_type,
                         LeadTimeQuote.logistics_source).filter(LeadTimeQuote.quote_key.in_(keys[offset:offset+400]))
        if lower is not None:
            query = query.filter(LeadTimeQuote.captured_at >= lower, LeadTimeQuote.captured_at < upper)
        for quote in query:
            for item in keyed[quote.quote_key]:
                if quote.status == 'invalid_postal_code' or quote.error == 'ZIP_CODE_NOT_FOUND':
                    item.update(logistics_status='CEP não localizado', logistics_note='A consulta não reconheceu este CEP.')
                    continue
                if item['logistics_status'] == 'Sem estoque':
                    continue
                fresh = quote.captured_at and (not item.get('last_update') or quote.captured_at >= item['last_update'])
                if fresh and quote.status == 'quoted' and quote.shipping_cost is not None and quote.delivery_max_days is not None:
                    # Copy the service together. A freight charge and a deadline
                    # from different methods cannot form a new quotation.
                    item.update({field: getattr(quote, field) for field in LOGISTICS_FIELDS if hasattr(quote, field)})
                    item.update(logistics_status='Cotação completa', logistics_note='Não se aplica', logistics_captured_at=quote.captured_at)
                elif quote.error:
                    item['logistics_note'] = quote.error
                elif quote.status in {'queued', 'running'}:
                    item['logistics_note'] = 'Cotação solicitada, aguardando confirmação da loja.'


def complete_workbook(db, job_id=None, fresh_after=None, date_from=None, date_to=None):
    from routes.products import _latest_products_query,_serialize_results
    job=db.get(ExtractionJob,job_id) if job_id else None
    if job_id and job is None:raise HTTPException(404,'Extração não encontrada.')
    skus=[item.sku for item in job.items] if job else None
    ceps=json.loads(job.postal_codes_json or '[]') if job else []
    stores=json.loads(job.stores_json or '[]') if job else []
    sellers=json.loads(job.sellers_json or '{}') if job else {}
    from services.store_registry import find_target_store
    store_labels=[(find_target_store(value) or {}).get('label',value) for value in stores]
    from services.price_observations import verified_observations,LOCAL
    observations=verified_observations(db.query(OfferObservation))
    if skus:observations=observations.filter(OfferObservation.sku.in_(skus))
    if ceps:observations=observations.filter(OfferObservation.postal_code.in_(ceps))
    if store_labels:observations=observations.filter(OfferObservation.source.in_(store_labels))
    if fresh_after:observations=observations.filter(OfferObservation.observed_at>=fresh_after)
    lower=upper=None
    if date_from or date_to:
        start,end=date_from or date_to,date_to or date_from
        if end<start: raise HTTPException(400,'A data final deve ser igual ou posterior à data inicial.')
        lower=datetime.combine(start,time.min,tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        upper=datetime.combine(end+timedelta(days=1),time.min,tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        observations=observations.filter(OfferObservation.observed_at>=lower,OfferObservation.observed_at<upper)
        from services.dated_export import observed_offers
        saved=observed_offers(db,observations)
    else:
        latest=_latest_products_query(db,sku_filter=skus)
        if skus:latest=latest.filter(Product.sku.in_(skus))
        if ceps:latest=latest.filter(PriceHistory.destination_zip.in_(ceps))
        if store_labels:latest=latest.filter(PriceHistory.source.in_(store_labels))
        if fresh_after:latest=latest.filter(PriceHistory.timestamp>=fresh_after)
        saved=_serialize_results(latest.all(),db,resolve_redirects=False)
    offers=[item for item in saved if (not skus or item.get('sku') in skus) and (not ceps or item.get('destination_zip') in ceps) and (not store_labels or item.get('source') in store_labels)]
    offers=[item for item in offers if seller_matches(sellers,item)]
    logistics_for_export(db, offers, lower, upper)
    with ExportWorkbook() as book:
        fields=[('sku','SKU'),('name','Produto'),('brand','Marca'),('source','Loja'),('store','Lojista'),('current_price','Preço'),('cash_price','À vista'),('installment_price','A prazo'),('installment_count','Parcelas'),('installment_value','Valor parcela'),('shipping_cost','Frete'),('delivery_min_days','Prazo mínimo'),('delivery_max_days','Prazo máximo'),('delivery_days_type','Tipo de dias'),('destination_zip','CEP'),('availability','Disponibilidade'),('fulfillment_type','Logística 2P/3P'),('fulfilled_by','Entregue por'),('subfamily','Subfamília'),('sku_category','Categoria'),('cluster','Cluster'),('market_type','Mercado'),('last_update','Atualizado em (UTC)'),('url','Oferta')]
        fields.extend([('logistics_status','Status da logística'),('logistics_note','Motivo da lacuna')])
        detail=book.create_sheet('Database');append(detail,[label for _,label in fields])
        for item in offers:append(detail,[('Sem estoque' if key=='current_price' and not offer_available(item) else item.get(key)) for key,_ in fields])
        offer_sheet=book.create_sheet('Ofertas');append(offer_sheet,['SKU','CEP','Produto','Loja','Lojista','Preço','Frete','Total + frete','Prazo máximo','Disponibilidade','URL'])
        for item in offers:
            price=item.get('current_price') if offer_available(item) else None;freight=item.get('shipping_cost')
            append(offer_sheet,[item.get('sku'),item.get('destination_zip'),item.get('name'),item.get('source'),item.get('store'),price,freight,price+freight if price is not None and freight is not None else None,item.get('delivery_max_days'),'Disponível' if offer_available(item) else 'Sem estoque',item.get('url')])
        grouped=defaultdict(list);by_store=defaultdict(list)
        for item in offers:
            grouped[(item.get('sku'),item.get('destination_zip'))].append(item)
            by_store[(item.get('sku'),item.get('destination_zip'),item.get('source'))].append(item)
        analytics=book.create_sheet('Analytics por SKU');append(analytics,['SKU','CEP','Ofertas','Lojas','Lojistas','Preço mínimo','Preço médio','Preço máximo','Melhor lojista','Diferença','Variação (%)'])
        for (sku,cep),items in sorted(grouped.items(),key=str):
            priced=[item for item in items if offer_available(item) and item.get('current_price') is not None];prices=[item['current_price'] for item in priced];best=min(priced,key=lambda item:item['current_price'],default={});minimum=min(prices) if prices else None;average=mean(prices) if prices else None
            append(analytics,[sku,cep,len(items),len({x.get('source') for x in items}),len({(x.get('source'),x.get('store')) for x in items}),minimum,average,max(prices) if prices else None,best.get('store'),average-minimum if prices else None,(average-minimum)/minimum*100 if prices and minimum else None])
        ranking=book.create_sheet('Preço por loja');append(ranking,['SKU','CEP','Loja','Lojistas','Ofertas','Mínimo','Médio','Máximo'])
        for (sku,cep,source),items in sorted(by_store.items(),key=str):
            prices=[item['current_price'] for item in items if offer_available(item) and item.get('current_price') is not None]
            append(ranking,[sku,cep,source,len({item.get('store') for item in items}),len(items),min(prices) if prices else None,mean(prices) if prices else None,max(prices) if prices else None])
        ranking.add_price_chart()
        comparison=book.create_sheet('Comparar lojas');sources=sorted({item.get('source') or '' for item in offers});append(comparison,['SKU','CEP',*sources])
        for (sku,cep),items in sorted(grouped.items(),key=str):append(comparison,[sku,cep,*[min((item['current_price'] for item in items if item.get('source')==source and offer_available(item) and item.get('current_price') is not None),default=None) for source in sources]])
        quotes=db.query(LeadTimeQuote).filter(~exists().where(
            (ProductSource.sku==LeadTimeQuote.sku)&(ProductSource.url==LeadTimeQuote.url)
            &(ProductSource.seller==LeadTimeQuote.seller)&ProductSource.validation_status.in_(HIDDEN_CURRENT_SOURCE_STATUSES)))
        if skus:quotes=quotes.filter(LeadTimeQuote.sku.in_(skus))
        if ceps:quotes=quotes.filter(LeadTimeQuote.postal_code.in_(ceps))
        if store_labels:quotes=quotes.filter(LeadTimeQuote.source.in_(store_labels))
        if fresh_after:quotes=quotes.filter(LeadTimeQuote.captured_at>=fresh_after)
        if lower is not None:quotes=quotes.filter(LeadTimeQuote.captured_at>=lower,LeadTimeQuote.captured_at<upper)
        quotes=quotes.order_by(LeadTimeQuote.sku,LeadTimeQuote.source,LeadTimeQuote.seller,LeadTimeQuote.postal_code).yield_per(500)
        if lower is not None:
            from services.dated_export import historical_quotes
            quotes=historical_quotes(db,offers,quotes)
        lead=book.create_sheet('Lead Time detalhes');append(lead,['SKU','Loja','Lojista','UF','CEP','Status','Frete','Prazo mínimo','Prazo máximo','Tipo de dias','Serviço','Cotado em (Brasília)','Oferta','Motivo'])
        freight=book.create_sheet('Frete por estado');append(freight,['SKU','UF','CEP','Loja','Lojista','Frete','Prazo máximo','Tipo de dias','Status','Cotado em (Brasília)'])
        for row in quotes:
            if not seller_matches(sellers,row): continue
            status = STATUS.get(row.status, row.status)
            append(lead,[row.sku,row.source,row.seller,row.uf,row.postal_code,status,row.shipping_cost,row.delivery_min_days,row.delivery_max_days,row.delivery_days_type,row.shipping_method,_excel_datetime(row.captured_at),row.url,row.error])
            append(freight,[row.sku,row.uf,row.postal_code,row.source,row.seller,row.shipping_cost,row.delivery_max_days,row.delivery_days_type,status,_excel_datetime(row.captured_at)])
        history=book.create_sheet('Histórico de preços');append(history,['Data (Brasília)','SKU','Loja','Lojista','CEP','Preço','Frete','Prazo máximo','Disponibilidade','Mudou','Origem','Oferta'])
        for row in observations.order_by(OfferObservation.observed_at).yield_per(500):
            if not seller_matches(sellers,row): continue
            snap=json.loads(row.snapshot_json);stamp=_excel_datetime(row.observed_at);append(history,[stamp,row.sku,row.source,row.seller,row.postal_code,row.price,snap.get('shipping_cost'),snap.get('delivery_max_days'),row.availability,'Sim' if row.changed else 'Não',row.origin,row.url])
        return finish(book)
