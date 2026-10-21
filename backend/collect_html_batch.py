"""Supplement a precise queue with HTTP scraping across independent stores.

One request stream per store, no browsers, no invented stock/prices. This does
not complete queue items or claim that blocked pages were searched successfully.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from queue import Queue, Empty
import time

from database import SessionLocal
from models.product import KnownSku, Product, PriceHistory, ProductSource
from crawlers.paged_discovery import fetch_search_page, search_url
from crawlers.registry import collect_product_source
from services.product_resolver import identity_from_models, resolve_candidate
from services.product_sources import upsert_product_source, mark_source_result
from services.precise_extraction import _valid_offer
from services.scraper_service import _save_result
from services.store_registry import TARGET_STORES


def collect_store(store, identities, messages):
    for identity in identities:
        visited = set()
        url = search_url(store["key"], identity.sku)
        diagnostics = {}
        count = 0
        try:
            while url and url not in visited:
                visited.add(url)
                candidates, next_url, ok = fetch_search_page(store["key"], url, 6, diagnostics=diagnostics)
                for candidate in candidates:
                    match = resolve_candidate(identity, candidate)
                    if not match.accepted and not candidate.get('_requires_pdp_verification'):
                        continue
                    source = ProductSource(store=store["key"], sku=identity.sku,
                                           url=candidate["url"], seller=candidate.get("seller"))
                    result = collect_product_source(source, identity, "01001000", timeout=6)
                    if not match.accepted:
                        confirmed = next((item for item in result.get('items', []) if _valid_offer(identity, item)), None)
                        if not confirmed:
                            continue
                        candidate = confirmed
                        match = resolve_candidate(identity, candidate)
                    messages.put((identity, store, candidate, match, result))
                    count += 1
                if not ok:
                    break
                url = next_url
                time.sleep(.25)
        except Exception as exc:
            diagnostics = {"status": "ERROR", "reason": str(exc)[:300]}
        messages.put((identity.sku, store["key"], len(visited), count, diagnostics))
        time.sleep(.25)


def persist_candidate(identity, store, candidate, match, result):
    with SessionLocal() as db:
        source = upsert_product_source(db, identity, store["key"], candidate, match)
        items = result.get("items") or []
        saved = 0
        for item in items:
            item["sku"] = identity.sku
            if _valid_offer(identity, item):
                seller_source = upsert_product_source(db, identity, store['key'], item, resolve_candidate(identity, item))
                if seller_source:
                    mark_source_result(seller_source, 'OK')
                saved += _save_result(item, db)
        # A live search card with a price is a real observation, but is not a
        # successful PDP or a confirmed shipping quote.
        if not items:
            item = {**candidate, "sku": identity.sku, "source": store["label"],
                    "store": candidate.get("seller") or store["label"],
                    "brand": identity.brand, "category": None, "image_url": candidate.get("image_url"),
                    "discovery_source": "html_batch_listing"}
            if result.get("status") == "OUT_OF_STOCK":
                item.update(price=None, availability="out_of_stock", discovery_source="html_batch_pdp")
            if _valid_offer(identity, item):
                saved += _save_result(item, db)
        if source:
            mark_source_result(source, result.get("status") or "PARSER_ERROR", result.get("reason"))
        db.commit()
        return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skus_file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--only-missing", action="store_true")
    args = parser.parse_args()
    skus = list(dict.fromkeys(Path(args.skus_file).read_text().split()))
    identities = []
    with SessionLocal() as db:
        for sku in skus:
            if args.only_missing and db.query(PriceHistory.id).join(Product).filter(Product.sku == sku).first():
                continue
            known = db.query(KnownSku).filter_by(sku=sku).first()
            identities.append(identity_from_models(known, db.query(Product).filter_by(sku=sku).all()))
    state = {"skus": [x.sku for x in identities], "saved_observations": 0, "completed_store_searches": [], "errors": []}
    messages = Queue()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=len(TARGET_STORES)) as pool:
        futures = [pool.submit(collect_store, store, identities, messages) for store in TARGET_STORES]
        while not all(f.done() for f in futures) or not messages.empty():
            try:
                message = messages.get(timeout=1)
            except Empty:
                continue
            if isinstance(message[0], str):
                sku, store, pages, candidates, diagnostics = message
                state["completed_store_searches"].append(dict(sku=sku, store=store, pages=pages,
                                                             candidates=candidates, diagnostics=diagnostics))
            else:
                try:
                    state["saved_observations"] += persist_candidate(*message)
                except Exception as exc:
                    state["errors"].append({"sku": message[0].sku, "reason": str(exc)[:400]})
            state["updated_at_utc"] = datetime.utcnow().isoformat() + "Z"
            temp = output.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(output)
        for future in futures:
            future.result()
    print(json.dumps({"skus": len(identities), "searches": len(state["completed_store_searches"]),
                      "saved_observations": state["saved_observations"], "errors": len(state["errors"])}))


if __name__ == "__main__":
    main()
