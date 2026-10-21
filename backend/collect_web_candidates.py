"""Verify manually discovered URLs with live store scraping; never import snippets."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
from threading import BoundedSemaphore
from urllib.parse import urlparse

from collect_html_batch import persist_candidate
from crawlers.paged_discovery import STORE_HOSTS
from crawlers.registry import collect_product_source
from database import SessionLocal
from models.product import KnownSku, Product, ProductSource
from services.product_resolver import identity_from_models, resolve_candidate
from services.product_sources import source_identifiers
from services.store_registry import TARGET_STORES


def verified_candidates(rows, identities):
    seen = set()
    for row in rows:
        url = row.get("url", "")
        parsed = urlparse(url)
        store = next((s for s in TARGET_STORES if parsed.hostname and (
            parsed.hostname == STORE_HOSTS[s["key"]] or parsed.hostname.endswith("." + STORE_HOSTS[s["key"]])
        )), None)
        if not store or parsed.scheme != "https" or re.search(r"/(busca|search|buscar|loja)(?:/|$)", parsed.path):
            continue
        for identity in identities:
            if identity.sku.lower() not in (row.get("name", "") + " " + url).lower():
                continue
            candidate = {"name": str(row.get("name") or ""), "url": url,
                         "_seller_unknown": True, "discovery_method": "manual_web_url"}
            match = resolve_candidate(identity, candidate)
            key = (identity.sku, url)
            if match.accepted and key not in seen:
                seen.add(key)
                yield identity, store, candidate, match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates_file")
    parser.add_argument("skus_file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    skus = Path(args.skus_file).read_text().split()
    with SessionLocal() as db:
        identities = [identity_from_models(db.query(KnownSku).filter_by(sku=sku).one(),
                                         db.query(Product).filter_by(sku=sku).all()) for sku in skus]
    rows = json.loads(Path(args.candidates_file).read_text(encoding="utf-8"))
    tasks = list(verified_candidates(rows, identities))
    slots = {s["key"]: BoundedSemaphore(2) for s in TARGET_STORES}
    state = {"candidates": len(tasks), "checked": [], "saved_observations": 0}
    def collect(task):
        identity, store, candidate, match = task
        with slots[store["key"]]:
            source = ProductSource(store=store["key"], sku=identity.sku, url=candidate["url"],
                                   **source_identifiers(candidate))
            return collect_product_source(source, identity, "01001000", timeout=6)
    output = Path(args.output)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(collect, task): task for task in tasks}
        for future in as_completed(futures):
            identity, store, candidate, match = futures[future]
            try:
                result = future.result()
                saved = persist_candidate(identity, store, candidate, match, result)
                state["saved_observations"] += saved
                state["checked"].append({"sku": identity.sku, "url": candidate["url"],
                                         "status": result.get("status"), "saved": saved,
                                         "reason": result.get("reason")})
            except Exception as exc:
                state["checked"].append({"sku": identity.sku, "url": candidate["url"], "status": "ERROR", "reason": str(exc)[:300]})
            temp = output.with_suffix(".tmp")
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(output)
    print(json.dumps({"candidates": len(tasks), "checked": len(state["checked"]), "saved": state["saved_observations"]}))


if __name__ == "__main__":
    main()
