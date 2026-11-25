"""Reconstruct dated exports from immutable observations, never from today's price."""
import json
from types import SimpleNamespace
from sqlalchemy import func
from models.product import OfferObservation, LeadTimeQuote
from services.structured_products import offer_available
from services.seller_selection import seller_key


def observed_offers(db, query):
    from routes.products import _known_sku_metadata_map, _apply_item_metadata
    ranked = query.with_entities(OfferObservation.id.label('id'),func.row_number().over(
        partition_by=OfferObservation.offer_key,
        order_by=(OfferObservation.observed_at.desc(),OfferObservation.id.desc()),
    ).label('position')).subquery()
    rows=db.query(OfferObservation).join(ranked,ranked.c.id==OfferObservation.id).filter(ranked.c.position==1)
    offers=[]
    for row in rows.yield_per(500):
        snapshot=json.loads(row.snapshot_json)
        offers.append({**snapshot,'sku':row.sku,'name':row.name,'brand':row.brand,
            'source':row.source,'store':row.seller,'seller':row.seller,'url':row.url,
            'current_price':row.price,'availability':row.availability,
            'destination_zip':row.postal_code,'last_update':row.observed_at})
    metadata=_known_sku_metadata_map(db,list({item['sku'] for item in offers})) if offers else {}
    for item in offers: _apply_item_metadata(item,metadata)
    return offers


def historical_quotes(db, offers, quotes):
    from services.store_registry import find_target_store
    def key(row):
        store=find_target_store(row.source)
        return (row.sku,row.source,seller_key(store['key'] if store else '',row.seller),row.url,row.postal_code)
    ceps={item.get('destination_zip') for item in offers if item.get('destination_zip')}
    states=dict(db.query(LeadTimeQuote.postal_code,LeadTimeQuote.uf).filter(LeadTimeQuote.postal_code.in_(ceps)).distinct()) if ceps else {}
    merged={}
    for item in offers:
        if not item.get('destination_zip'): continue
        available=offer_available(item)
        row=SimpleNamespace(sku=item['sku'],source=item['source'],seller=item['store'],url=item['url'],
            postal_code=item['destination_zip'],uf=states.get(item['destination_zip']),
            captured_at=item['last_update'],shipping_cost=item.get('shipping_cost'),
            delivery_min_days=item.get('delivery_min_days'),delivery_max_days=item.get('delivery_max_days'),
            delivery_days_type=item.get('delivery_days_type'),shipping_method=item.get('shipping_method'),
            status='out_of_stock' if not available else 'quoted' if item.get('shipping_cost') is not None and item.get('delivery_max_days') is not None else 'not_quoted',
            error=None)
        merged[key(row)]=row
    for row in quotes:
        old=merged.get(key(row))
        if old is None or row.captured_at>=old.captured_at: merged[key(row)]=row
    return sorted(merged.values(),key=lambda row:(row.sku,row.source,row.seller or '',row.postal_code))
