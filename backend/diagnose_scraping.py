"""Read-only live samples for the configured stores. No paid discovery providers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import Semaphore
import time
from types import SimpleNamespace

from crawlers.base import collect_http
from crawlers.paged_discovery import fetch_search_page, search_url
from crawlers.profiles import DEFAULT_PROFILE, STORE_PROFILES
from services.offer_enrichment import enrich_offer, normalize_postal_code
from services.playwright_fallback import collect_product_with_playwright, collect_shipping_with_playwright
from services.product_resolver import ProductIdentity, resolve_candidate
from services.product_sources import source_identifiers
from services.store_registry import TARGET_STORES
from services.structured_products import merge_offer_details, offer_available


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", nargs="+", default=["W11199342", "W11299803", "W10531315"])
    parser.add_argument("--cep", default="01001000")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--shipping", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".logs" / "scraping-audit.json")
    args = parser.parse_args()
    cep = normalize_postal_code(args.cep)
    if not cep:
        parser.error("CEP invalido")
    database = Path(__file__).resolve().parent / "precos.db"
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        identities = {}
        sources = {}
        for sku in args.skus:
            row = db.execute("SELECT * FROM known_skus WHERE sku=?", (sku,)).fetchone()
            identities[sku] = ProductIdentity(
                sku=sku, model=(row["model"] if row else None) or sku,
                name=row["description"] if row else None, brand=row["brand"] if row else None,
                ean=row["ean"] if row else None, mpn=row["mpn"] if row else None,
            )
            sources[sku] = [dict(row) for row in db.execute(
                "SELECT * FROM product_sources WHERE sku=? AND active=1 "
                "ORDER BY (validation_status='ok') DESC, last_attempted_at DESC", (sku,),
            )]
    slots = {store["key"]: Semaphore(1) for store in TARGET_STORES}

    def sample(sku, store):
        with slots[store["key"]]:
            started = time.monotonic()
            identity = identities[sku]
            known = [row for row in sources[sku] if row["store"] == store["key"]
                     and resolve_candidate(identity, {"name": row["listing_name"] or identity.name,
                                                      "url": row["url"], "model": row["model"]}).accepted]
            diagnostics = {}
            if known:
                source = SimpleNamespace(**known[0])
            else:
                candidates, _next, success = fetch_search_page(
                    store["key"], search_url(store["key"], sku), args.timeout, diagnostics=diagnostics,
                )
                if not success and args.browser and "429" not in str(diagnostics.get("reason")):
                    candidates, _next, success = fetch_search_page(
                        store["key"], search_url(store["key"], sku), args.timeout,
                        diagnostics=diagnostics, use_browser=True,
                    )
                candidate = next((item for item in candidates if resolve_candidate(identity, item).accepted), None)
                if not candidate:
                    status = diagnostics.get("status", "NOT_FOUND")
                    return {"sku": sku, "store": store["key"], "status": "NOT_FOUND" if status == "OK" else status,
                            "reason": diagnostics.get("reason") or "Busca sem candidato confirmado para este SKU",
                            "seconds": round(time.monotonic() - started, 1), "offers": []}
                source = SimpleNamespace(
                    store=store["key"], seller=candidate.get("seller") or store["label"],
                    url=candidate["url"], brand=candidate.get("brand"),
                    **source_identifiers(candidate),
                )
            result = collect_http(source, identity, None, STORE_PROFILES.get(store["key"], DEFAULT_PROFILE), args.timeout)
            http_status = result["status"]
            if args.browser and result["status"] in {"BLOCKED", "TIMEOUT", "PARSER_ERROR"} and not result.get("_retry_after"):
                browser_result = collect_product_with_playwright(source, identity, cep, args.timeout, include_shipping=args.shipping)
                if browser_result["status"] == "OK":
                    result = browser_result
                else:
                    result["reason"] = f"{result.get('reason', '')}; navegador: {browser_result.get('reason', browser_result['status'])}"
            items = result.get("items", [])
            enriched = []
            urls = [item["url"] for item in items]
            for item in items:
                if offer_available(item) and urls.count(item["url"]) == 1 and store["key"] != "mercado_livre" and item.get("_logistics_attempted_zip") != cep:
                    item = enrich_offer(item, cep, force_refresh=True)
                    if args.shipping and (item.get("shipping_cost") is None or item.get("delivery_max_days") is None):
                        quote = collect_shipping_with_playwright(item["url"], cep, args.timeout)
                        if not quote.get("_blocked_status"):
                            item = merge_offer_details(item, quote)
                enriched.append({key: item.get(key) for key in (
                    "name", "seller", "url", "price", "cash_price", "installment_price", "image_url",
                    "shipping_cost", "delivery_min_days", "delivery_max_days", "delivery_days_type", "destination_zip", "availability", "logistics_source", "discovery_source",
                )})
            return {"sku": sku, "store": store["key"], "status": result["status"], "http_status": http_status,
                    "reason": result.get("reason"), "seconds": round(time.monotonic() - started, 1), "offers": enriched}

    records = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(sample, sku, store): (sku, store) for sku in args.skus for store in TARGET_STORES}
        for future in as_completed(futures):
            sku, store = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {"sku": sku, "store": store["key"], "status": "ERROR", "reason": str(exc)[:300], "offers": []}
            records.append(record)
            print(f"{sku} {store['key']}: {record['status']}, {len(record['offers'])} ofertas, {record.get('seconds', '?')}s", flush=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "postal_code": cep,
        "sampling": "Uma pagina por SKU e loja; nao representa todos os anuncios disponiveis",
        "database_modified": False, "paid_providers_used": False,
        "records": sorted(records, key=lambda record: (record["sku"], record["store"])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Relatorio: {args.output}", flush=True)


if __name__ == "__main__":
    main()
