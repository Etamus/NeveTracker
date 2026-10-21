"""Read-only report of a persistent extraction job, including SKUs with no offers.

Usage: python report_extraction.py JOB_ID --output ../.logs/extraction-report --watch
The workbook is replaced atomically and may be locked temporarily by Excel.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import time

from openpyxl import Workbook

from database import SessionLocal
from models.product import ExtractionJob, PriceHistory, Product, ProductSource, ScrapeCursor
from routes.products import _analytics_sheet_query, _serialize_results
from services.extraction_queue import serialize_job, TERMINAL_STATUSES
from services.store_registry import TARGET_STORES


def write_report(job_id, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        job = db.query(ExtractionJob).filter_by(id=job_id).one()
        state = serialize_job(job, include_items=True)
        skus = [item.sku for item in job.items]
        offers = _serialize_results(_analytics_sheet_query(db, ",".join(skus), "", None, None).all(),
                                    db, resolve_redirects=False, fresh_after=job.created_at)
        sources = db.query(ProductSource).filter(ProductSource.sku.in_(skus)).all()
        cursors = db.query(ScrapeCursor).filter(ScrapeCursor.sku.in_(skus)).all()
        fresh_rows = db.query(Product.sku, PriceHistory.id).join(PriceHistory).filter(
            Product.sku.in_(skus), PriceHistory.timestamp >= job.created_at,
            PriceHistory.is_cached.is_(False),
        ).all()
        fresh_counts = Counter(sku for sku, _ in fresh_rows)
        offer_counts = Counter(offer["sku"] for offer in offers)
        stores = [store for store in TARGET_STORES if job.scope == "all" or job.scope == store["scope"]]
        status_rows, coverage_rows = [], []
        for item in job.items:
            attempted = set()
            for store in stores:
                pages = [c for c in cursors if c.sku == item.sku and c.store == store["key"]]
                listings = [s for s in sources if s.sku == item.sku and s.store == store["key"]]
                recent_pages = [c for c in pages if c.last_attempted_at and c.last_attempted_at >= job.created_at]
                recent_sources = [s for s in listings if s.last_attempted_at and s.last_attempted_at >= job.created_at]
                if recent_pages or recent_sources:
                    attempted.add(store["key"])
                diagnostics = sorted({c.last_status or "unknown" for c in recent_pages}
                                     | {s.validation_status or "unknown" for s in recent_sources})
                coverage_rows.append([item.sku, store["label"], bool(recent_pages or recent_sources),
                                      len(recent_pages), len(recent_sources), len(listings),
                                      ", ".join(diagnostics),
                                      "; ".join(sorted({c.failure_reason for c in recent_pages if c.failure_reason}))[:3000]])
            status_rows.append([item.sku, item.status, item.attempts or 0, len(attempted), len(stores),
                                fresh_counts[item.sku], offer_counts[item.sku], item.message or ""])
        state["report_generated_at_utc"] = datetime.utcnow().isoformat() + "Z"
        state["fresh_observations"] = sum(fresh_counts.values())
        state["skus_with_fresh_observations"] = len(fresh_counts)
        state["saved_offers_including_older_cache"] = len(offers)

    temp = output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output.with_suffix(".json"))
    workbook = Workbook()
    status = workbook.active
    status.title = "SKUs"
    status.append(["SKU", "Estado da fila", "Rodadas", "Lojas tentadas nesta execução", "Lojas previstas",
                   "Observações novas nesta execução", "Ofertas salvas (inclui anteriores)", "Diagnóstico"])
    for row in status_rows:
        status.append(row)
    coverage = workbook.create_sheet("Cobertura por loja")
    coverage.append(["SKU", "Loja", "Tentada nesta execução", "Páginas tentadas", "Anúncios tentados",
                     "Fontes conhecidas (inclui anteriores)", "Estados", "Falhas"])
    for row in coverage_rows:
        coverage.append(row)
    details = workbook.create_sheet("Ofertas salvas")
    fields = ["sku", "name", "source", "store", "current_price", "cash_price", "installment_price",
              "availability", "shipping_cost", "delivery_min_days", "delivery_max_days",
              "destination_zip", "last_update", "is_cached", "url"]
    details.append(fields)
    for offer in offers:
        details.append([str(offer.get(field)) if isinstance(offer.get(field), datetime) else offer.get(field)
                        for field in fields])
    for sheet in workbook:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            sheet.column_dimensions[cell.column_letter].width = 24
        # External titles and diagnostics are untrusted spreadsheet text.
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith(("=", "+", "-", "@")):
                    cell.data_type = "s"
    xlsx_temp = output.with_suffix(".tmp.xlsx")
    workbook.save(xlsx_temp)
    workbook.close()
    try:
        xlsx_temp.replace(output.with_suffix(".xlsx"))
    except PermissionError:
        # JSON continues to refresh while a user has the workbook open in Excel.
        xlsx_temp.unlink(missing_ok=True)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("--output", default="../.logs/extraction-report")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        result = write_report(args.job_id, args.output)
        print(json.dumps({key: result[key] for key in (
            "status", "processed_skus", "visited_skus", "store_steps_done", "fresh_observations",
        )}), flush=True)
        if not args.watch or result["status"] in TERMINAL_STATUSES:
            break
        time.sleep(30)
