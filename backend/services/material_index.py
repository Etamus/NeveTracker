"""One material index shared by management, selectors and saved-offer views."""
from sqlalchemy import select, union, func, or_, exists
import re
from models.product import KnownSku, Product, PriceHistory, ProductSource, LeadTimeQuote


def material_query(db, search=''):
    catalog=union(select(KnownSku.sku),select(Product.sku),select(ProductSource.sku),select(LeadTimeQuote.sku)).subquery()
    query=db.query(catalog.c.sku).filter(catalog.c.sku.isnot(None),catalog.c.sku!='')
    if search.strip():
        term='%'+search.strip().replace('%',r'\%').replace('_',r'\_')+'%'
        if re.fullmatch(r'(?=.*\d)[A-Za-z0-9-]+',search.strip()):
            return query.filter(catalog.c.sku.ilike(term,escape='\\'))
        query=query.filter(or_(catalog.c.sku.ilike(term,escape='\\'),
            exists().where((Product.sku==catalog.c.sku)&Product.name.ilike(term,escape='\\')),
            exists().where((KnownSku.sku==catalog.c.sku)&KnownSku.description.ilike(term,escape='\\'))))
    return query


def material_status_map(db, skus=None):
    from routes.products import _latest_products_query,_known_sku_metadata_map
    keys=list(dict.fromkeys(skus)) if skus else [row[0] for row in material_query(db).all()]
    if not keys:return {}
    metadata=_known_sku_metadata_map(db,keys)
    rows=_latest_products_query(db,keys).filter(Product.sku.in_(keys)).with_entities(
        Product.sku,func.count(func.distinct(Product.id)),
        func.count(func.distinct(func.coalesce(PriceHistory.source,'')+'|'+PriceHistory.store+'|'+PriceHistory.url)),
        func.count(func.distinct(func.coalesce(PriceHistory.source,'')+'|'+PriceHistory.store)),
        func.max(PriceHistory.timestamp)).group_by(Product.sku).all()
    stats={row[0]:row for row in rows}
    return {sku:{'sku':sku,'in_database':bool(stats.get(sku)),
        'product_count':stats[sku][1] if sku in stats else 0,
        'offer_count':stats[sku][2] if sku in stats else 0,
        'store_count':stats[sku][3] if sku in stats else 0,
        'last_update':stats[sku][4] if sku in stats else None,
        **metadata.get(sku,{})} for sku in keys}
