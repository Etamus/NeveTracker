"""Immutable observations and explicitly identified daily carry-forward reports."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone, time
import hashlib
import heapq
import json
from statistics import mean
from typing import NamedTuple

from sqlalchemy import and_, exists, func, or_, select
from database import begin_report_snapshot
from models.product import OfferObservation, PriceHistory, Product, ProductSource
from services.product_resolver import canonical_brand
from services.product_sources import HIDDEN_CURRENT_SOURCE_STATUSES
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


def verified_observations(query, current=False):
    return query.filter(~exists().where(
        (ProductSource.sku==OfferObservation.sku)
        & (ProductSource.url==OfferObservation.url)
        & (ProductSource.seller==OfferObservation.seller)
        & (ProductSource.validation_status.in_(HIDDEN_CURRENT_SOURCE_STATUSES if current else ('invalid_match',)))
    ))


def report_search_predicate(search):
    words = dict.fromkeys(search.split())
    if not words:
        return None
    columns = (OfferObservation.sku, OfferObservation.name, OfferObservation.seller,
               OfferObservation.source, OfferObservation.brand)
    return and_(*(or_(*(column.icontains(word, autoescape=True) for column in columns)) for word in words))


class _HistoryObservation(NamedTuple):
    id: int
    offer_key: str
    sku: str
    source: str
    seller: str
    url: str
    postal_code: str | None
    price: float | None
    availability: str | None
    observed_at: datetime
    changed: bool
    matches_search: bool = True


class _HistoryDay(NamedTuple):
    date: str
    observation: _HistoryObservation
    previous_price: float | None
    checks: tuple[_HistoryObservation, ...]
    predecessor_id: int | None


def _stock_state(value):
    state = str(value or '').strip().rsplit('/', 1)[-1].lower()
    if state in {'instock', 'in_stock', 'in stock', 'em estoque', 'disponivel', 'disponível',
                 'limitedavailability', 'limited_availability'}:
        return 'in_stock'
    if state and not offer_available({'availability': state}) and state != 'discontinued':
        return 'out_of_stock'
    return None


def _history_details(db, days):
    # Only decompress snapshots for rows displayed on this page and the
    # immediately preceding check needed to identify fields changed that day.
    ids = set()
    for day in days:
        ids.add(day.observation.id)
        ids.update(row.id for row in day.checks)
        if day.checks and day.predecessor_id is not None:
            ids.add(day.predecessor_id)
    ids = sorted(ids)
    details = {}
    for offset in range(0, len(ids), 500):
        query = db.query(OfferObservation.id, OfferObservation.name, OfferObservation.brand,
                         OfferObservation.origin, OfferObservation.snapshot_json, OfferObservation.observed_at)
        for row in query.filter(OfferObservation.id.in_(ids[offset:offset + 500])):
            details[row.id] = (row.name, row.brand, row.origin, json.loads(row.snapshot_json), row.observed_at)
    items = []
    for day in days:
        row = day.observation
        name, brand, origin, snapshot, _ = details[row.id]
        predecessor = details.get(day.predecessor_id) if day.checks else None
        previous = predecessor[3] if predecessor else None
        previous_at = predecessor[4] if predecessor else None
        changed_fields = set()
        stock_events = []
        for check in day.checks:
            current = details[check.id][3]
            if previous is not None:
                changed_fields.update(key for key in current if previous.get(key) != current[key])
                before, after = _stock_state(previous.get('availability')), _stock_state(current.get('availability'))
                if before and after and before != after:
                    stock_events.append({'kind': 'rupture' if after == 'out_of_stock' else 'restock',
                                         'observed_at': check.observed_at, 'previous_observed_at': previous_at})
            previous = current
            previous_at = check.observed_at
        price = row.price if offer_available({'availability': row.availability}) else None
        difference = price - day.previous_price if price is not None and day.previous_price is not None else None
        items.append({'date': day.date, 'sku': row.sku, 'name': name, 'brand': brand,
                      'source': row.source, 'seller': row.seller, 'url': row.url, 'postal_code': row.postal_code,
                      'price': price, 'previous_price': day.previous_price, 'difference': difference,
                      'difference_percent': difference / day.previous_price * 100 if difference is not None and day.previous_price else None,
                      'shipping_cost': snapshot.get('shipping_cost'), 'availability': row.availability,
                      'delivery_min_days': snapshot.get('delivery_min_days'), 'delivery_max_days': snapshot.get('delivery_max_days'),
                      'delivery_days_type': snapshot.get('delivery_days_type'), 'details': snapshot,
                      'changed_fields': sorted(changed_fields), 'observed_at': row.observed_at, 'stock_events': stock_events,
                      'carried': not day.checks, 'checks': len(day.checks),
                      'changed': any(check.changed for check in day.checks), 'origin': origin})
    return items


def history_report(db, start, end, skus=None, sources=None, brands=None, carry=True, page=1, page_size=50, all_dates=False, search=''):
    if end < start or (not all_dates and (end-start).days > 365):
        raise ValueError('Escolha um período de até 366 dias, com data final após a inicial.')
    begin_report_snapshot(db)
    upper = datetime.combine(end+timedelta(days=1), time.min, tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
    query = verified_observations(db.query(OfferObservation)).filter(OfferObservation.observed_at < upper)
    if skus:
        query = query.filter(OfferObservation.sku.in_(skus))
    if sources:
        query = query.filter(OfferObservation.source.in_([_label(value) for value in sources]))
    if brands:
        query = query.filter(OfferObservation.brand.in_([canonical_brand(value) for value in brands]))
    matches = report_search_predicate(search)
    if matches is not None:
        # Read the whole timeline of matching offers. A renamed product must
        # supersede its previous title even when its new name no longer matches.
        keys = query.with_entities(OfferObservation.offer_key).filter(matches).distinct().statement
        query = query.filter(OfferObservation.offer_key.in_(keys))
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
    columns = [getattr(OfferObservation, name) for name in _HistoryObservation._fields if name != 'matches_search']
    if matches is not None:
        columns.append(matches.label('matches_search'))
    for row in query.with_entities(*columns).order_by(OfferObservation.observed_at, OfferObservation.id).yield_per(2000):
        observation = _HistoryObservation(*row)
        grouped[observation.offer_key].append(observation)
    day_stats = defaultdict(dict)
    day_changes = defaultdict(int)
    ranked = []
    total = 0
    page_end = page * page_size if page_size is not None else None
    for observations in grouped.values():
        by_day = defaultdict(list)
        for observation in observations:
            by_day[local_day(observation.observed_at)].append(observation)
        first_day = next(iter(by_day))
        baseline = by_day[first_day][-1] if first_day < start else None
        current = baseline
        prior_price = baseline.price if baseline and offer_available({'availability': baseline.availability}) else None
        day = max(first_day, start)
        while day <= end:
            checks = by_day.get(day, ())
            previous = current
            if checks:
                current = checks[-1]
            if current and (carry or checks):
                price = current.price if offer_available({'availability': current.availability}) else None
                previous_price, prior_price = prior_price, price
                if not current.matches_search:
                    day += timedelta(days=1)
                    continue
                day_label = day.isoformat()
                rank = (day_label, current.sku, current.source, current.seller, current.postal_code or '', -total)
                total += 1
                if page_end is None or len(ranked) < page_end or rank > ranked[0][0]:
                    item = _HistoryDay(day_label, current, previous_price, tuple(checks), previous.id if previous else None)
                    if page_end is None:
                        ranked.append((rank, item))
                    elif len(ranked) < page_end:
                        heapq.heappush(ranked, (rank, item))
                    else:
                        heapq.heapreplace(ranked, (rank, item))
                if price is not None:
                    day_stats[day][current.sku, current.source, current.seller, current.url] = price
                day_changes[day] += sum(row.changed for row in checks)
            day += timedelta(days=1)
    series = []
    day = start
    while day <= end:
        prices = list(day_stats[day].values())
        series.append({'date': day.isoformat(), 'minimum': min(prices) if prices else None,
            'average': mean(prices) if prices else None, 'maximum': max(prices) if prices else None,
            'offers': len(prices), 'changes': day_changes[day]})
        day += timedelta(days=1)
    offset = page_end - page_size if page_end is not None else 0
    days = [item for _, item in sorted(ranked, reverse=True)[offset:]]
    return {'items': _history_details(db, days), 'total': total, 'page': page, 'page_size': page_size,
            'series': series, 'start': start.isoformat(), 'end': end.isoformat(),
            'first_observation': db.query(OfferObservation.observed_at).order_by(OfferObservation.observed_at).limit(1).scalar()}


def compare_groups(db, dimension, left, right, skus=None, sources=None, brands=None, start=None, end=None, search=''):
    if dimension not in {'source', 'brand'} or not left or not right or left == right:
        raise ValueError('Escolha duas lojas ou marcas diferentes.')
    rows = verified_observations(db.query(OfferObservation), current=not (start or end))
    groups = [_label(left), _label(right)] if dimension == 'source' else [canonical_brand(left), canonical_brand(right)]
    if dimension == 'source':
        rows = rows.filter(OfferObservation.source.in_(groups))
    if start and end and end<start:
        raise ValueError('A data final deve ser igual ou posterior à data inicial.')
    if start:
        lower=datetime.combine(start,time.min,tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        rows=rows.filter(OfferObservation.observed_at>=lower)
    if end:
        upper=datetime.combine(end+timedelta(days=1),time.min,tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        rows=rows.filter(OfferObservation.observed_at<upper)
    if skus:
        rows = rows.filter(OfferObservation.sku.in_(skus))
    if sources:
        rows = rows.filter(OfferObservation.source.in_([_label(value) for value in sources]))
    latest_rows = rows.with_entities(OfferObservation.id.label('id'), func.row_number().over(
        partition_by=(OfferObservation.sku, OfferObservation.source, OfferObservation.seller, OfferObservation.url),
        order_by=(OfferObservation.observed_at.desc(), OfferObservation.id.desc()),
    ).label('position')).subquery()
    rows = db.query(OfferObservation.sku, OfferObservation.source, OfferObservation.seller, OfferObservation.brand,
                    OfferObservation.price, OfferObservation.availability, OfferObservation.observed_at).filter(OfferObservation.id.in_(
        select(latest_rows.c.id).where(latest_rows.c.position == 1)))
    if brands:
        rows = rows.filter(OfferObservation.brand.in_([canonical_brand(value) for value in brands]))
    if dimension == 'brand':
        # The brand may have been corrected since a previous check of the same ad.
        rows = rows.filter(OfferObservation.brand.in_(groups))
    matches = report_search_predicate(search)
    if matches is not None:
        rows = rows.filter(matches)
    latest = rows.all()
    result, by_sku = [], defaultdict(lambda: [[], []])
    for index, group in enumerate(groups):
        found = [row for row in latest if getattr(row, dimension) == group]
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
