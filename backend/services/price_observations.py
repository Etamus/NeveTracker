"""Immutable observations and explicitly identified daily carry-forward reports."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone, time
import hashlib
import json
from statistics import mean

from sqlalchemy import exists, func, or_, select
from models.product import OfferObservation, PriceHistory, Product
from services.product_resolver import canonical_brand
from services.structured_products import offer_available
from services.store_registry import find_target_store

LOCAL = timezone(timedelta(hours=-3))
FIELDS = ('price', 'cash_price', 'installment_price', 'installment_count', 'installment_value',
          'shipping_cost', 'shipping_method', 'delivery_min_days', 'delivery_max_days', 'delivery_days_type',
          'destination_zip', 'availability', 'fulfillment_type', 'fulfilled_by', 'name', 'brand',
          'payment_difference', 'handling_min_days', 'handling_max_days', 'logistics_source',
          'fulfillment_evidence', 'discovery_source')


def record_observation(db, item, observed_at, origin='collection', legacy_price_id=None):
    if item.get('is_cached'):
        return None
    db.flush()
    store = find_target_store(item.get('source'), item.get('store'), item.get('url'))
    source = store['label'] if store else item.get('source') or item.get('store') or 'Não informado'
    seller = item.get('seller') or item.get('store') or source
    sku = item.get('sku') or ''
    snapshot = {key: item.get(key) for key in FIELDS}
    snapshot['brand'] = canonical_brand(snapshot['brand'])
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    key = hashlib.sha256(json.dumps([sku, source, seller, item['url'], item.get('destination_zip') or '']).encode()).hexdigest()
    previous = db.query(OfferObservation).filter_by(offer_key=key).order_by(OfferObservation.observed_at.desc(), OfferObservation.id.desc()).first()
    previous_snapshot = json.loads(previous.snapshot_json) if previous else None
    changed = previous_snapshot is not None and any(previous_snapshot.get(field) != snapshot.get(field) for field in FIELDS)
    row = OfferObservation(legacy_price_id=legacy_price_id, offer_key=key, sku=sku, source=source,
        seller=seller, brand=snapshot['brand'], name=item.get('name'), url=item['url'], price=item.get('price'),
        availability=item.get('availability'), postal_code=item.get('destination_zip'), observed_at=observed_at,
        changed=changed, origin=origin, snapshot_json=encoded)
    db.add(row)
    return row


def backfill_observations(db):
    """Only dates actually present in old history can be recovered."""
    rows = db.query(Product, PriceHistory).join(PriceHistory, PriceHistory.product_id == Product.id).filter(
        PriceHistory.is_cached.is_(False),
        ~exists().where(OfferObservation.legacy_price_id == PriceHistory.id),
    ).order_by(PriceHistory.timestamp, PriceHistory.id).all()
    for product, price in rows:
        item = {key: getattr(price, key, None) for key in FIELDS}
        item.update(sku=product.sku, name=product.name, brand=product.brand, price=price.price,
                    url=price.url or '', store=price.store, source=price.source)
        record_observation(db, item, price.timestamp or datetime.utcnow(), origin='legacy', legacy_price_id=price.id)
    db.commit()
    return len(rows)


def local_day(value):
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL).date()


def _label(value):
    store = find_target_store(value)
    return store['label'] if store else value


def history_report(db, start, end, skus=None, sources=None, brands=None, carry=True, page=1, page_size=50):
    if end < start or (end-start).days > 365:
        raise ValueError('Escolha um período de até 366 dias, com data final após a inicial.')
    upper = datetime.combine(end+timedelta(days=1), time.min, tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
    query = db.query(OfferObservation).filter(OfferObservation.observed_at < upper)
    if skus:
        query = query.filter(OfferObservation.sku.in_(skus))
    if sources:
        query = query.filter(OfferObservation.source.in_([_label(value) for value in sources]))
    if brands:
        query = query.filter(OfferObservation.brand.in_([canonical_brand(value) for value in brands]))
    lower = datetime.combine(start, time.min, tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
    baseline_rows = query.filter(OfferObservation.observed_at < lower).with_entities(
        OfferObservation.id.label('id'), func.row_number().over(
            partition_by=OfferObservation.offer_key,
            order_by=(OfferObservation.observed_at.desc(), OfferObservation.id.desc()),
        ).label('position')).subquery()
    query = query.filter(or_(OfferObservation.observed_at >= lower, OfferObservation.id.in_(
        select(baseline_rows.c.id).where(baseline_rows.c.position == 1))))
    # Keep the most recent observation before the requested interval as a
    # baseline. An old price is identified as carried, never as a new check.
    grouped = defaultdict(list)
    for row in query.order_by(OfferObservation.observed_at, OfferObservation.id):
        grouped[row.offer_key].append(row)
    rows = []
    day_stats = defaultdict(list)
    day_changes = defaultdict(int)
    for observations in grouped.values():
        field_changes = {}
        previous_snapshot = None
        for observation in observations:
            snapshot = json.loads(observation.snapshot_json)
            field_changes[observation.id] = [key for key in snapshot if previous_snapshot is not None and previous_snapshot.get(key) != snapshot[key]]
            previous_snapshot = snapshot
        index, current = 0, None
        baseline = next((row for row in reversed(observations) if local_day(row.observed_at) < start), None)
        prior_price = baseline.price if baseline and offer_available({'availability': baseline.availability}) else None
        day = start
        while day <= end:
            today_observations = []
            while index < len(observations) and local_day(observations[index].observed_at) <= day:
                current = observations[index]
                if local_day(current.observed_at) == day:
                    today_observations.append(current)
                index += 1
            if current and (carry or today_observations):
                available = offer_available({'availability': current.availability})
                price = current.price if available else None
                difference = price-prior_price if price is not None and prior_price is not None else None
                snap = json.loads(current.snapshot_json)
                rows.append({'date': day.isoformat(), 'sku': current.sku, 'name': current.name,
                    'brand': current.brand, 'source': current.source, 'seller': current.seller, 'url': current.url,
                    'postal_code': current.postal_code, 'price': price, 'previous_price': prior_price,
                    'difference': difference, 'difference_percent': difference/prior_price*100 if difference is not None and prior_price else None,
                    'shipping_cost': snap.get('shipping_cost'), 'availability': current.availability,
                    'delivery_min_days': snap.get('delivery_min_days'), 'delivery_max_days': snap.get('delivery_max_days'),
                    'delivery_days_type': snap.get('delivery_days_type'), 'details': snap,
                    'changed_fields': sorted({key for row in today_observations for key in field_changes[row.id]}),
                    'observed_at': current.observed_at, 'carried': not bool(today_observations),
                    'checks': len(today_observations), 'changed': any(row.changed for row in today_observations),
                    'origin': current.origin})
                if price is not None:
                    day_stats[day].append((current.sku, current.source, current.seller, current.url, price))
                day_changes[day] += sum(row.changed for row in today_observations)
                # Previous day means the last displayed day, not the previous
                # distinct value; unchanged daily prices still have a baseline.
                prior_price = price
            day += timedelta(days=1)
    rows.sort(key=lambda row: (row['date'], row['sku'], row['source'], row['seller'], row['postal_code'] or ''), reverse=True)
    series = []
    day = start
    while day <= end:
        unique = {entry[:4]: entry[4] for entry in day_stats[day]}
        prices = list(unique.values())
        series.append({'date': day.isoformat(), 'minimum': min(prices) if prices else None,
            'average': mean(prices) if prices else None, 'maximum': max(prices) if prices else None,
            'offers': len(prices), 'changes': day_changes[day]})
        day += timedelta(days=1)
    total = len(rows)
    selected = rows if page_size is None else rows[(page-1)*page_size:page*page_size]
    return {'items': selected, 'total': total, 'page': page, 'page_size': page_size,
            'series': series, 'start': start.isoformat(), 'end': end.isoformat(),
            'first_observation': db.query(OfferObservation.observed_at).order_by(OfferObservation.observed_at).limit(1).scalar()}


def compare_groups(db, dimension, left, right, skus=None, sources=None, brands=None):
    if dimension not in {'source', 'brand'} or not left or not right or left == right:
        raise ValueError('Escolha duas lojas ou marcas diferentes.')
    rows = db.query(OfferObservation).order_by(OfferObservation.observed_at.desc(), OfferObservation.id.desc())
    if skus:
        rows = rows.filter(OfferObservation.sku.in_(skus))
    if sources:
        rows = rows.filter(OfferObservation.source.in_([_label(value) for value in sources]))
    latest_rows = rows.with_entities(OfferObservation.id.label('id'), func.row_number().over(
        partition_by=(OfferObservation.sku, OfferObservation.source, OfferObservation.seller, OfferObservation.url),
        order_by=(OfferObservation.observed_at.desc(), OfferObservation.id.desc()),
    ).label('position')).subquery()
    rows = db.query(OfferObservation).filter(OfferObservation.id.in_(
        select(latest_rows.c.id).where(latest_rows.c.position == 1)))
    if brands:
        rows = rows.filter(OfferObservation.brand.in_([canonical_brand(value) for value in brands]))
    field = OfferObservation.source if dimension == 'source' else OfferObservation.brand
    groups = [_label(left), _label(right)] if dimension == 'source' else [canonical_brand(left), canonical_brand(right)]
    rows = rows.filter(field.in_(groups))
    latest = {}
    for row in rows:
        # A seller's most recent price wins, irrespective of how many CEPs
        # were quoted for the same advertisement.
        key = (row.sku, row.source, row.seller, row.url)
        latest.setdefault(key, row)
    result, by_sku = [], defaultdict(lambda: [[], []])
    for index, group in enumerate(groups):
        found = [row for row in latest.values() if getattr(row, dimension) == group]
        available = [row for row in found if row.price is not None and offer_available({'availability': row.availability})]
        for row in available:
            by_sku[row.sku][index].append(row.price)
        result.append({'label': group, 'offers': len(found), 'available': len(available),
            'skus': len({row.sku for row in found}), 'sellers': len({(row.source, row.seller) for row in found}),
            'minimum': min((row.price for row in available), default=None),
            'average': mean([row.price for row in available]) if available else None,
            'last_update': max((row.observed_at for row in found), default=None)})
    common = []
    for sku, pair in sorted(by_sku.items()):
        a, b = (min(values) if values else None for values in pair)
        common.append({'sku': sku, 'left': a, 'right': b, 'difference': b-a if a is not None and b is not None else None})
    return {'dimension': dimension, 'groups': result, 'items': common,
            'common_skus': sum(row['left'] is not None and row['right'] is not None for row in common)}
