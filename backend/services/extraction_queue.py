from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from sqlalchemy.orm import object_session, Session
from sqlalchemy import text, or_
from sqlalchemy.exc import OperationalError
from fastapi import HTTPException

from database import SessionLocal
from models.product import ExtractionJob, ExtractionJobItem, ExtractionSchedule, KnownSku, ScrapeCursor, ProductSource
from services.offer_enrichment import normalize_postal_code
from services.extraction_resources import (
    ExtractionCancelled, cancellation_scope, check_cancelled, interruptible_sleep,
)


BATCH_SIZE = 30
PRECISE_SKU_SLICE_SECONDS = 180
PRECISE_SKU_SLICE_ROUNDS = 12
MAX_JOB_SKUS = 5000
MAX_ITEM_ATTEMPTS = 2
SQLITE_SKU_WORKER_LIMIT = 8
ACTIVE_STATUSES = ("queued", "running", "waiting_access", "paused")
TERMINAL_STATUSES = ("completed", "completed_with_errors", "cancelled", "failed")
JOB_RETENTION_DAYS = 30
MAX_RETAINED_JOBS = 200
DEFER_MARKETPLACES = True
LAST_STORES = frozenset({'mercado_livre', 'shopee'})


def _requested_stores(job):
    from services.store_registry import TARGET_STORES
    requested = json.loads(job.stores_json or '[]')
    return [store['key'] for store in TARGET_STORES
            if (job.scope == 'all' or store.get('scope') == job.scope)
            and (not requested or store['key'] in requested)]


def _bounded_env_float(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


SKU_PAUSE_SECONDS = _bounded_env_float(
    "NEVETRACKER_QUEUE_SKU_PAUSE_SECONDS", 0.75, 0.25, 5.0
)
BATCH_PAUSE_SECONDS = _bounded_env_float(
    "NEVETRACKER_QUEUE_BATCH_PAUSE_SECONDS", 5.0, 1.0, 60.0
)
PRECISE_BATCH_PAUSE_SECONDS = _bounded_env_float(
    "NEVETRACKER_QUEUE_PRECISE_PAUSE_SECONDS",
    BATCH_PAUSE_SECONDS if "NEVETRACKER_QUEUE_BATCH_PAUSE_SECONDS" in os.environ else 0.5,
    0.25, 60.0,
)

_processor = None
_started = False
_start_lock = threading.Lock()
_wake_event = threading.Event()
_enqueue_lock = threading.RLock()
_batch_events = {}
_batch_events_lock = threading.Lock()


def configure_extraction_processor(processor):
    global _processor
    _processor = processor


def _request_key(
    skus, kind, force_refresh, refresh_if_stale, postal_code, scope, mode, run_key
):
    parts = [
        kind,
        str(bool(force_refresh)),
        str(bool(refresh_if_stale)),
        postal_code or "",
        scope,
        mode,
        run_key or "",
        *sorted(skus),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _priority_for_kind(kind):
    return {"single": 5, "manual": 10, "database": 20, "scheduled": 30}.get(
        kind, 20
    )


def _job_progress(job):
    total = max(0, job.total_skus or 0)
    processed = min(total, max(0, job.processed_skus or 0))
    return round((processed / total) * 100, 1) if total else 0.0


def serialize_job(job, include_items=False, *, include_metrics=True):
    from services.marketplace_intervention import suspension as pending
    from services.store_registry import TARGET_STORES
    requested=json.loads(job.stores_json or '[]') or [store['key'] for store in TARGET_STORES if job.scope=='all' or store.get('scope')==job.scope]
    waiting=sorted({store for item in job.items for store in json.loads(item.waiting_stores_json or '[]')} | {store for store in requested if pending(store)}) if job.status in ACTIVE_STATUSES else []
    visited = sum(bool(item.attempts) for item in job.items)
    rounds = sum(item.attempts or 0 for item in job.items)
    completion = _job_progress(job)
    work_progress = completion
    if total := job.total_skus:
        staged = sum(item.store_phase == 'marketplaces' or item.status in {'completed', 'cached', 'failed'}
                     for item in job.items if item.store_phase != 'legacy')
        work_progress = max(work_progress, round(50 * staged / total + .5 * completion, 1))
    checked = 0
    stage_total = 0
    if job.mode == 'precise':
        from services.store_registry import TARGET_STORES
        keys = {store['key'] for store in TARGET_STORES if job.scope == 'all' or store.get('scope') == job.scope}
        requested_stores=json.loads(job.stores_json or '[]')
        if requested_stores:keys.intersection_update(requested_stores)
        stage_total = (job.total_skus or 0) * len(keys)
        session = object_session(job)
        if session is not None and job.created_at:
            pairs = set(session.query(ScrapeCursor.sku, ScrapeCursor.store).join(
                ExtractionJobItem, ExtractionJobItem.sku == ScrapeCursor.sku,
            ).filter(ExtractionJobItem.job_id == job.id, ScrapeCursor.store.in_(keys),
                     ScrapeCursor.last_checked_at >= job.created_at).distinct().all())
            pairs.update(session.query(ProductSource.sku, ProductSource.store).join(
                ExtractionJobItem, ExtractionJobItem.sku == ProductSource.sku,
            ).filter(ExtractionJobItem.job_id == job.id, ProductSource.store.in_(keys),
                     ProductSource.last_attempted_at >= job.created_at).distinct().all())
            checked = len(pairs)
        if job.total_skus:
            # Starts, store checkpoints and final completion are separate milestones.
            # This estimate cannot reach 100% while any SKU remains unfinished.
            stage_fraction = min(1, checked / stage_total) if stage_total else 0
            work_progress = round(5 * visited / job.total_skus + 45 * stage_fraction + .5 * completion, 1)
            work_progress = max(completion, min(99.9, work_progress)) if completion < 100 else 100.0
    active = [{'sku': item.sku, 'phase': item.store_phase, 'message': item.message or '',
               'progress': json.loads(item.progress_json or '{}')}
              for item in job.items if item.status == 'running']
    quotes = {}
    observed_skus = observations_saved = 0
    session = object_session(job)
    if include_metrics and session is not None and job.created_at and job.mode == 'precise':
        from sqlalchemy import func
        from models.product import LeadTimeQuote, OfferObservation
        quote_query = session.query(LeadTimeQuote.status, func.count(LeadTimeQuote.id)).join(
            ExtractionJobItem, ExtractionJobItem.sku == LeadTimeQuote.sku,
        ).filter(ExtractionJobItem.job_id == job.id,
                 LeadTimeQuote.captured_at >= job.created_at,
                 LeadTimeQuote.status != 'invalid_postal_code',
                 LeadTimeQuote.source.in_([store['label'] for store in TARGET_STORES if store['key'] in requested]))
        if destinations := json.loads(job.postal_codes_json or '[]'):
            quote_query = quote_query.filter(LeadTimeQuote.postal_code.in_(destinations))
        quotes = dict(quote_query.group_by(LeadTimeQuote.status).all())
        observation_query = session.query(func.count(func.distinct(OfferObservation.sku)),func.count(OfferObservation.id)).join(
            ExtractionJobItem, ExtractionJobItem.sku == OfferObservation.sku,
        ).filter(ExtractionJobItem.job_id == job.id,OfferObservation.observed_at >= job.created_at,
                 OfferObservation.source.in_([store['label'] for store in TARGET_STORES if store['key'] in requested]))
        if destinations:
            observation_query=observation_query.filter(OfferObservation.postal_code.in_(destinations))
        observed_skus,observations_saved=observation_query.one()
        if seller_selection := json.loads(job.sellers_json or '{}'):
            from collections import Counter
            from services.seller_selection import seller_matches
            # Counters must not include another seller collected by an earlier
            # queued job whose observations fall after this job's creation time.
            quotes = dict(Counter(row.status for row in quote_query.with_entities(
                LeadTimeQuote.source,LeadTimeQuote.seller,LeadTimeQuote.status,LeadTimeQuote.url,
            ).yield_per(500) if seller_matches(seller_selection,row)))
            observed=set();observations_saved=0
            for row in observation_query.with_entities(OfferObservation.source,OfferObservation.seller,OfferObservation.sku,OfferObservation.url).yield_per(500):
                if seller_matches(seller_selection,row):
                    observed.add(row.sku);observations_saved+=1
            observed_skus=len(observed)
    from services.extraction_resources import snapshot
    response = {
        'active_skus': active,
        'general_skus_done': sum(item.store_phase == 'marketplaces' or item.status in {'completed','cached','failed'} for item in job.items),
        'resources': snapshot(),
        "waiting_access": [pending(store) or {'store':store,'status':'ready'} for store in waiting],
        "waiting_skus":sum(item.status=='waiting_access' for item in job.items),
        "id": job.id,
        "owner_user_id": job.owner_user_id,
        "api_with_scraping": job.api_with_scraping,
        "postal_codes": json.loads(job.postal_codes_json or '[]'),
        "stores": json.loads(job.stores_json or '[]'),
        "sellers": json.loads(job.sellers_json or '{}'),
        "kind": job.kind,
        "status": job.status,
        "scope": job.scope,
        "mode": job.mode,
        "force_refresh": job.force_refresh,
        "refresh_if_stale": job.refresh_if_stale,
        "batch_size": job.batch_size,
        "total_skus": job.total_skus,
        "processed_skus": job.processed_skus,
        "visited_skus": visited,
        "started_rounds": rounds,
        "store_steps_done": checked,
        "store_steps_total": stage_total,
        "work_progress_percent": work_progress,
        "success_count": job.success_count,
        "cached_count": job.cached_count,
        "error_count": job.error_count,
        "current_sku": job.current_sku,
        "message": job.message,
        "cancel_requested": job.cancel_requested,
        "progress_percent": _job_progress(job),
        "batches_total": (
            (job.total_skus + job.batch_size - 1) // job.batch_size
            if job.total_skus and job.batch_size
            else 0
        ),
        "batches_completed": (
            (job.processed_skus + job.batch_size - 1) // job.batch_size
            if job.processed_skus and job.batch_size
            else 0
        ),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
    }
    if include_metrics:
        response.update(quotes_checked=sum(quotes.values()), observed_skus=observed_skus,
                        observations_saved=observations_saved, quotes_complete=quotes.get('quoted', 0),
                        quotes_out_of_stock=quotes.get('out_of_stock', 0),
                        quotes_incomplete=quotes.get('failed', 0) + quotes.get('not_quoted', 0))
    if include_items:
        response["items"] = [
            {
                "sku": item.sku,
                "status": (
                    "error"
                    if item.status == "failed"
                    else "cached"
                    if item.status == "cached"
                    else "updated"
                    if item.status == "completed"
                    else item.status
                ),
                "message": item.message or "",
                "results_count": item.results_count,
                "stores_count": item.stores_count,
                "comparison_count": item.comparison_count,
                "in_database": item.in_database,
                "offer_count": item.offer_count,
                "store_count": item.store_count,
                "last_update": item.last_update,
                "attempts": item.attempts,
            }
            for item in job.items
        ]
    return response


def enqueue_extraction_job(
    skus,
    *,
    kind="manual",
    force_refresh=True,
    refresh_if_stale=False,
    postal_code=None,
    scope="all",
    mode="quick",
    schedule_run_key=None,
    owner_user_id=None,
    api_with_scraping=True,
    postal_codes=None,
    stores=None,
    sellers=None,
    register_materials=False,
):
    postal_code = normalize_postal_code(postal_code)
    normalized_skus = list(dict.fromkeys(sku for sku in skus if sku))
    if not normalized_skus:
        raise ValueError("Informe ao menos um SKU valido.")
    if len(normalized_skus) > MAX_JOB_SKUS:
        raise ValueError(f"Limite de {MAX_JOB_SKUS} SKUs por fila.")

    request_key = _request_key(
        normalized_skus,
        kind,
        force_refresh,
        refresh_if_stale,
        postal_code,
        scope,
        mode,
        schedule_run_key,
    )
    from schemas.product import SkuBatchRequest
    validated = SkuBatchRequest(skus=normalized_skus,postal_codes=postal_codes or [],stores=stores or [],sellers=sellers or {})
    postal_codes=validated.postal_codes
    stores=validated.stores
    sellers={key: names for key,names in validated.sellers.items() if not stores or key in stores}
    if postal_codes:postal_code=postal_codes[0]
    request_key = hashlib.sha256(f'{request_key}:{owner_user_id}:{api_with_scraping}:{postal_codes}:{stores}'.encode()).hexdigest()
    if sellers:
        from services.seller_selection import seller_key
        canonical={key:sorted(seller_key(key,name) for name in names) for key,names in sorted(sellers.items())}
        request_key=hashlib.sha256(f'{request_key}:{json.dumps(canonical,sort_keys=True)}'.encode()).hexdigest()
    db = SessionLocal()
    _enqueue_lock.acquire()
    try:
        # Serialize the check and insertion across both threads and SQLite
        # connections/processes. No network operation takes place in this lock.
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        elif db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(72491831)'))
        active_jobs = db.query(ExtractionJob).filter(ExtractionJob.status.in_(ACTIVE_STATUSES)).all()
        if any(job.owner_user_id != owner_user_id for job in active_jobs):
            raise HTTPException(409, 'Outro usuário está realizando uma extração. Aguarde a conclusão para iniciar a sua.')
        existing = (
            db.query(ExtractionJob)
            .filter(
                ExtractionJob.request_key == request_key,
                ExtractionJob.status.in_(ACTIVE_STATUSES),
            )
            .order_by(ExtractionJob.created_at.desc())
            .first()
        )
        if existing:
            return serialize_job(existing)

        job = ExtractionJob(
            id=str(uuid.uuid4()),
            owner_user_id=owner_user_id,
            api_with_scraping=True if mode != 'precise' else bool(api_with_scraping),
            postal_codes_json=json.dumps(postal_codes),
            stores_json=json.dumps(stores),
            sellers_json=json.dumps(sellers,ensure_ascii=False),
            kind=kind,
            status="queued",
            priority=_priority_for_kind(kind),
            request_key=request_key,
            scope=scope,
            mode=mode if mode in {"quick", "precise"} else "quick",
            force_refresh=bool(force_refresh),
            refresh_if_stale=bool(refresh_if_stale),
            postal_code=postal_code,
            schedule_run_key=schedule_run_key,
            batch_size=1 if mode == "precise" else BATCH_SIZE,
            total_skus=len(normalized_skus),
            message=f"{len(normalized_skus)} SKUs aguardando processamento.",
        )
        job.items = [
            ExtractionJobItem(sku=sku, position=index, store_phase='general' if DEFER_MARKETPLACES else 'legacy')
            for index, sku in enumerate(normalized_skus)
        ]
        db.add(job)
        if register_materials:
            for offset in range(0, len(normalized_skus), 400):
                batch = normalized_skus[offset:offset + 400]
                existing = {sku for (sku,) in db.query(KnownSku.sku).filter(KnownSku.sku.in_(batch))}
                db.add_all(KnownSku(sku=sku) for sku in batch if sku not in existing)
        db.commit()
        db.refresh(job)
        response = serialize_job(job)
    finally:
        db.close()
        _enqueue_lock.release()

    _wake_event.set()
    return response


def get_extraction_job(job_id, include_items=False, *, include_metrics=True):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).first()
        return serialize_job(job, include_items=include_items, include_metrics=include_metrics) if job else None
    finally:
        db.close()


def get_active_extraction_job():
    db = SessionLocal()
    try:
        job = (
            db.query(ExtractionJob)
            .filter(ExtractionJob.status.in_(ACTIVE_STATUSES))
            .order_by(ExtractionJob.priority.asc(), ExtractionJob.created_at.asc())
            .first()
        )
        return serialize_job(job) if job else None
    finally:
        db.close()


def cancel_extraction_job(job_id, owner_user_id=None, *, can_cancel_any=False):
    db = SessionLocal()
    try:
        job = db.get(ExtractionJob, job_id)
        if not job:
            return None
        if not can_cancel_any and (owner_user_id is None or job.owner_user_id != owner_user_id):
            raise HTTPException(403, 'Somente um administrador ou o usuário que iniciou a extração pode cancelá-la.')
        if job.status not in TERMINAL_STATUSES:
            # Signal the local collector before waiting for a database writer.
            # It might be the collector itself that is holding the reservation.
            with _batch_events_lock:
                event = _batch_events.get(job_id)
                if event is not None:
                    event.set()
            db.rollback()
            _begin_queue_write(db)
            job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).with_for_update().first()
            if not job or job.status in TERMINAL_STATUSES:
                return serialize_job(job) if job else None
            job.cancel_requested = True
            now = datetime.utcnow()
            running = db.query(ExtractionJobItem.id).filter_by(job_id=job_id, status="running").first()
            if running:
                job.message = "Cancelando tarefas em andamento."
                job.updated_at = now
            else:
                _finalize_cancelled_job(db, job, now)
            db.commit()
            with _batch_events_lock:
                event = _batch_events.get(job_id)
                if event is not None:
                    event.set()
            _wake_event.set()
            db.refresh(job)
        return serialize_job(job)
    finally:
        db.close()


def set_extraction_paused(job_id, owner_user_id, paused):
    db=SessionLocal()
    try:
        _begin_queue_write(db)
        job=db.query(ExtractionJob).filter_by(id=job_id).with_for_update().first()
        if not job:return None
        if owner_user_id is None or job.owner_user_id!=owner_user_id:
            raise HTTPException(403,'Somente o usuário que iniciou a extração pode pausar ou retomar a fila.')
        if job.status in TERMINAL_STATUSES or job.cancel_requested:
            raise HTTPException(409,'Esta extração não pode ser pausada ou retomada.')
        if paused:
            job.status='paused'
            job.message='Extração pausada. Os dados e checkpoints foram preservados.'
        elif job.status=='paused':
            job.status='queued'
            job.message='Retomando dos checkpoints salvos.'
        job.updated_at=datetime.utcnow();db.commit()
        _wake_event.set()
        return serialize_job(job)
    finally:db.close()


def _recover_interrupted_jobs():
    db = SessionLocal()
    try:
        from services.scrape_pagination import OPERATIONAL_KINDS
        db.query(ScrapeCursor).filter(ScrapeCursor.kind.notin_(OPERATIONAL_KINDS),
            ScrapeCursor.completed.is_(False)).update({ScrapeCursor.completed: True,
                ScrapeCursor.last_status: 'AUDIT_ONLY'}, synchronize_session=False)
        # Upgrade only unfinished work. Completed SKUs and offer/CEP checkpoints
        # are never reset, including jobs created by an older release.
        if DEFER_MARKETPLACES:
            for item in db.query(ExtractionJobItem).join(ExtractionJob).filter(
                ExtractionJob.status.in_(ACTIVE_STATUSES),
                ExtractionJobItem.status.in_(['queued', 'running', 'waiting_access']),
                ExtractionJobItem.store_phase == 'legacy',
            ):
                pending = set(json.loads(item.resume_stores_json or item.waiting_stores_json or '[]'))
                item.store_phase = 'marketplaces' if pending and pending <= LAST_STORES else 'general'
        interrupted_items = (
            db.query(ExtractionJobItem)
            .filter(ExtractionJobItem.status == "running")
            .all()
        )
        for item in interrupted_items:
            item.status = "queued"
            item.started_at = None

        interrupted_jobs = (
            db.query(ExtractionJob).filter(ExtractionJob.status == "running").all()
        )
        for job in interrupted_jobs:
            job.status = "queued"
            job.current_sku = None
            job.message = "Fila retomada apos reinicio do backend."
        db.flush()
        for job in db.query(ExtractionJob).filter(
            ExtractionJob.status.in_(ACTIVE_STATUSES), ExtractionJob.cancel_requested.is_(True),
        ):
            _finalize_cancelled_job(db, job, datetime.utcnow())
        db.commit()
    finally:
        db.close()


def _cleanup_old_jobs():
    db = SessionLocal()
    try:
        terminal_jobs = (
            db.query(ExtractionJob)
            .filter(ExtractionJob.status.in_(TERMINAL_STATUSES))
            .order_by(ExtractionJob.completed_at.desc(), ExtractionJob.created_at.desc())
            .all()
        )
        cutoff = datetime.utcnow() - timedelta(days=JOB_RETENTION_DAYS)
        for index, job in enumerate(terminal_jobs):
            reference_time = job.completed_at or job.created_at
            if index >= MAX_RETAINED_JOBS or reference_time < cutoff:
                db.delete(job)
        db.commit()
    finally:
        db.close()


def _next_job_id():
    db = SessionLocal()
    try:
        from services.marketplace_intervention import ready
        exhausted = _close_exhausted_transport_jobs(db)
        ready_stores = {store for store in LAST_STORES if store not in exhausted and ready(store)}
        for waiting in db.query(ExtractionJobItem).join(ExtractionJob).filter(
            ExtractionJob.status.in_(('queued','running','waiting_access')),ExtractionJobItem.status=='waiting_access').all():
            job=waiting.job
            stores=json.loads(waiting.waiting_stores_json or '[]')
            available = [store for store in stores if store in ready_stores]
            if available and not job.cancel_requested:
                # A cancellation may have committed since the waiting rows were read.
                updated = db.query(ExtractionJob).filter(
                    ExtractionJob.id == job.id,
                    ExtractionJob.status.in_(('queued', 'running', 'waiting_access')),
                    ExtractionJob.cancel_requested.is_(False),
                ).update({ExtractionJob.status: 'queued'}, synchronize_session=False)
                if updated:
                    waiting.status = 'queued'
                    waiting.resume_stores_json = json.dumps(available)
        db.commit()
        job = (
            db.query(ExtractionJob)
            .filter(ExtractionJob.status == "queued")
            .order_by(ExtractionJob.priority.asc(), ExtractionJob.created_at.asc())
            .first()
        )
        return job.id if job else None
    finally:
        db.close()


def _mark_item_running(job_id, item_id):
    db = SessionLocal()
    try:
        _begin_queue_write(db)
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).with_for_update().one()
        item = db.query(ExtractionJobItem).filter(ExtractionJobItem.id == item_id).one()
        if job.cancel_requested or job.status=='paused' or item.status != 'queued':
            return None
        stores = json.loads(item.resume_stores_json or job.stores_json or '[]')
        deferred = []
        if item.store_phase == 'general':
            requested = _requested_stores(job)
            stores = [store for store in requested if store not in LAST_STORES]
            deferred = [store for store in requested if store in LAST_STORES]
            if not stores:
                item.store_phase = 'marketplaces'
                stores, deferred = deferred, []
        elif item.store_phase == 'marketplaces' and not item.resume_stores_json:
            stores = [store for store in _requested_stores(job) if store in LAST_STORES]
        if item.resume_stores_json and stores and set(stores) <= LAST_STORES:
            from services.marketplace_intervention import waiting_stores
            suspended = set(waiting_stores(stores))
            stores = [store for store in stores if store not in suspended]
            if suspended:
                pending = set(json.loads(item.waiting_stores_json or '[]')) | suspended
                item.waiting_stores_json = json.dumps(sorted(pending))
                item.resume_stores_json = json.dumps(stores) if stores else None
            if not stores:
                item.status = 'waiting_access'
                item.message = 'Aguardando a loja liberar a próxima tentativa.'
                db.commit()
                return None
        now = datetime.utcnow()
        job.status = "running"
        job.started_at = job.started_at or now
        job.current_sku = item.sku
        stage_label = 'Mercado Livre e Shopee' if item.store_phase == 'marketplaces' else 'demais lojas'
        job.message = f"Processando {item.sku}: {stage_label}."
        item.status = "running"
        item.message = job.message
        item.progress_json = None
        with _progress_lock:
            _progress_times.pop((job_id,item.sku), None)
        item.attempts = (item.attempts or 0) + 1
        item.started_at = now
        db.commit()
        return {
            "job_id": job_id,
            "sku": item.sku,
            "kind": job.kind,
            "scope": job.scope,
            "mode": job.mode,
            "api_with_scraping": job.api_with_scraping,
            "force_refresh": job.force_refresh,
            "refresh_if_stale": job.refresh_if_stale,
            "postal_code": job.postal_code,
            "postal_codes": json.loads(job.postal_codes_json or '[]'),
            "stores": stores,
            "sellers": json.loads(job.sellers_json or '{}'),
            "store_phase": item.store_phase,
            "deferred_stores": deferred,
            "remaining_waiting_stores":json.loads(item.waiting_stores_json or '[]'),
            "attempt": item.attempts,
            "job_created_at": job.created_at,
        }
    finally:
        db.close()


def _finish_item(job_id, item_id, result):
    db = SessionLocal()
    try:
        _begin_queue_write(db)
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).with_for_update().one()
        item = db.query(ExtractionJobItem).filter(ExtractionJobItem.id == item_id).one()
        result_status = result.get("status") or "error"
        phase_done = item.store_phase == 'general' and result_status not in {'partial', 'error', 'waiting_access'}
        deferred = [store for store in _requested_stores(job) if store in LAST_STORES] if phase_done else []
        previous=set(json.loads(item.waiting_stores_json or '[]'))
        resumed=set(json.loads(item.resume_stores_json or '[]'))
        requested=set(result.get('_waiting_stores') or [])
        remaining=(previous-resumed)|requested if result_status!='partial' else previous|requested
        item.waiting_stores_json=json.dumps(sorted(remaining)) if remaining else None
        if result_status!='partial' and remaining:result_status='waiting_access'
        if result_status == 'partial' and remaining:
            from services.marketplace_intervention import suspension
            # A partial marketplace pass cannot make progress while every
            # remaining store is suspended. Preserve its cursors and wait.
            if resumed and resumed <= remaining and all(suspension(store) for store in resumed):
                result_status = 'waiting_access'
        transitioning = bool(deferred and result_status not in {'partial', 'error', 'waiting_access'})
        if transitioning:
            item.store_phase = 'marketplaces'
            item.resume_stores_json = json.dumps(deferred)
            item.status = 'queued'
        elif result_status == "partial":
            item.status = "queued"
        elif result_status=='waiting_access':
            item.status='waiting_access';item.resume_stores_json=None
        elif result_status == "error":
            item.status = "failed"
            job.error_count = (job.error_count or 0) + 1
        elif result_status == "cached":
            item.status = "cached"
            job.cached_count = (job.cached_count or 0) + 1
            job.success_count = (job.success_count or 0) + 1
        else:
            item.status = "completed"
            job.success_count = (job.success_count or 0) + 1

        item.message = str(result.get("message") or "")[:4000]
        if '_offers' in result:
            offers=json.loads(item.results_json or '[]')
            merged={(offer.get('sku'),offer.get('source'),offer.get('store'),offer.get('url'),offer.get('destination_zip')):offer for offer in [*offers,*result['_offers']]}
            item.results_json=json.dumps(list(merged.values()),ensure_ascii=False,default=str)
            result={**result,'results_count':len(merged),'stores_count':len({offer.get('source') for offer in merged.values()})}
        for field in (
            "results_count",
            "stores_count",
            "comparison_count",
            "offer_count",
            "store_count",
        ):
            setattr(item, field, int(result.get(field) or 0))
        item.in_database = bool(result.get("in_database"))
        item.last_update = result.get("last_update")
        if not transitioning and result_status not in {"partial","waiting_access"}:
            item.resume_stores_json=None
            item.completed_at = datetime.utcnow()
            job.processed_skus = (job.processed_skus or 0) + 1
        job.current_sku = None
        job.updated_at = datetime.utcnow()
        db.commit()
        return 'partial' if transitioning else result_status
    finally:
        db.close()


def _begin_queue_write(db):
    # Obtain SQLite's writer reservation before reading shared job counters.
    # Short mutations only: no network call takes place inside this transaction.
    if db.get_bind().dialect.name == 'sqlite':
        db.execute(text('BEGIN IMMEDIATE'))


def _sku_workers(db, precise):
    if str(db.get_bind().url) in {'sqlite://', 'sqlite:///:memory:'}:
        return 1  # A StaticPool fixture has one connection, not a concurrent DB.
    from services.crawler_control import get_control
    from services.extraction_resources import effective_workers
    get_control(db)
    workers = effective_workers('precise_sku_workers' if precise else 'quick_sku_workers')
    # SKU collectors hold sessions during I/O. Leave room in SQLite's pool for
    # discovery, progress writes and the API even when hardware can run more.
    return min(workers, SQLITE_SKU_WORKER_LIMIT) if db.get_bind().dialect.name == 'sqlite' else workers


def _job_item_ids(job_id, exclude=(), *, phase=None, limit=None):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).one()
        precise = job.mode == "precise"
        if job.cancel_requested or job.status == 'paused':
            return [], precise
        candidates = db.query(ExtractionJobItem.id, ExtractionJobItem.sku, ExtractionJobItem.attempts,
                              ExtractionJobItem.store_phase, ExtractionJobItem.position, ExtractionJobItem.started_at)
        candidates = candidates.filter_by(job_id=job_id, status='queued')
        if phase is None:
            # Standard mode keeps the global barrier between the two phases.
            general_pending = db.query(ExtractionJobItem.id).filter_by(job_id=job_id, store_phase='general').filter(
                ExtractionJobItem.status.in_(['queued','running'])).first() is not None
            if general_pending:
                phase = 'general'
        if phase is not None:
            candidates = candidates.filter_by(store_phase=phase)
        rows = candidates.order_by(ExtractionJobItem.attempts.asc(), ExtractionJobItem.position.asc()).all()
        rows = [row for row in rows if row.id not in exclude]
        if precise and any(row.attempts for row in rows):
            from services.store_registry import TARGET_STORES
            from services.precise_extraction import _source_due
            from services.scrape_pagination import browser_retry_due, OPERATIONAL_KINDS
            keys = {store['key'] for store in TARGET_STORES if job.scope == 'all' or store['scope'] == job.scope}
            requested_stores=json.loads(job.stores_json or '[]')
            if requested_stores:keys.intersection_update(requested_stores)
            general = phase == 'general'
            if general:
                keys.difference_update(LAST_STORES)
            elif rows and all(row.store_phase=='marketplaces' for row in rows):
                keys.intersection_update(LAST_STORES)
            skus = [row.sku for row in rows]
            checked, actionable, pending_sources = {}, set(), set()
            for cursor in db.query(ScrapeCursor).filter(ScrapeCursor.sku.in_(skus), ScrapeCursor.store.in_(keys), ScrapeCursor.kind.in_(OPERATIONAL_KINDS)):
                if cursor.last_checked_at and cursor.last_checked_at >= job.created_at:
                    checked.setdefault(cursor.sku, set()).add(cursor.store)
                if not cursor.completed and (cursor.last_attempted_at is None or cursor.last_status in {'OK', 'OK_BROWSER'} or browser_retry_due(cursor)):
                    actionable.add(cursor.sku)
            now = datetime.utcnow()
            for source in db.query(ProductSource).filter(ProductSource.sku.in_(skus), ProductSource.store.in_(keys), ProductSource.active.is_(True)):
                if source.validation_status=='ok' and source.last_attempted_at and source.last_attempted_at>=job.created_at:
                    checked.setdefault(source.sku,set()).add(source.store)
                if _source_due(source, job.created_at, now):
                    pending_sources.add(source.sku)
            ready = {sku for sku, stores in checked.items() if stores == keys and sku not in actionable and sku not in pending_sources}
            latest_finished=db.query(ExtractionJobItem.started_at).filter(
                ExtractionJobItem.job_id==job_id, ExtractionJobItem.started_at.isnot(None),
                or_(ExtractionJobItem.status.in_(['completed','cached','failed']),
                    ExtractionJobItem.store_phase=='marketplaces' if general else False),
            ).order_by(ExtractionJobItem.started_at.desc()).first()
            # Finish already checked materials before starting more. Actual
            # processing still validates completion; no counters are fabricated.
            # Continue material already in progress before opening hundreds of
            # other SKUs. Human-access waits are excluded above and cannot stall
            # this list. The time slice still allows other jobs to be considered.
            rows.sort(key=lambda row: (row.sku not in ready,
                                       row.attempts >= PRECISE_SKU_SLICE_ROUNDS and not (
                                           latest_finished and row.started_at and latest_finished.started_at >= row.started_at),
                                       not bool(row.attempts), row.position))
        if limit is None:
            limit = _sku_workers(db, precise) if precise else BATCH_SIZE
        ids = [row.id for row in rows[:limit]]
        return ids, precise
    finally:
        db.close()


_progress_times = {}
_progress_lock = threading.Lock()


def update_job_progress(job_id, sku, message, db=None):
    """Persist the current phase so long requests remain visible to the UI."""
    check_cancelled()
    now = time.monotonic()
    with _progress_lock:
        previous = _progress_times.get((job_id, sku), 0)
        if now - previous < .75:
            return
        _progress_times[(job_id, sku)] = now
        if len(_progress_times) > 10000:
            _progress_times.clear()
    # A UI heartbeat must never commit the collector's data or fail its SKU.
    # With SQLite a parent write transaction already owns the only writer.
    if db is not None and db.get_bind().dialect.name == 'sqlite':
        if db.connection().connection.driver_connection.in_transaction:
            return
    factory_session = SessionLocal()
    bind = factory_session.get_bind()
    factory_session.close()
    connection = bind.connect()
    db = Session(bind=connection)
    sqlite = bind.dialect.name == 'sqlite'
    original_timeout = None
    try:
        if sqlite:
            raw = connection.connection.driver_connection
            original_timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
            raw.execute('PRAGMA busy_timeout=100')
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).first()
        if job and job.status == "running" and not job.cancel_requested:
            item = db.query(ExtractionJobItem).filter_by(job_id=job_id, sku=sku, status='running').first()
            if item:
                item.message = str(message)[:400]
                if hasattr(message, 'details'):
                    item.progress_json = json.dumps(message.details, ensure_ascii=False)
            job.message = item.message if item else str(message)[:400]
            job.updated_at = datetime.utcnow()
            db.commit()
    except OperationalError:
        db.rollback()  # Progress is retried on the next tick; collected data is untouched.
    finally:
        db.close()
        if original_timeout is not None:
            connection.connection.driver_connection.execute(f'PRAGMA busy_timeout={int(original_timeout)}')
        connection.close()


def _record_schedule_completion(db, job, completed_at):
    if not job.schedule_run_key:
        return
    schedule = db.query(ExtractionSchedule).filter(ExtractionSchedule.id == 1).first()
    if schedule:
        schedule.last_run_key = job.schedule_run_key
        schedule.last_run = completed_at


def _close_exhausted_transport_jobs(db, job_ids=None):
    """End fully blocked transport waits while preserving collected offers."""
    from services.marketplace_intervention import transport_retries_exhausted
    from services.store_registry import TARGET_STORES

    exhausted = {store for store in LAST_STORES if transport_retries_exhausted(store)}
    if not exhausted:
        return exhausted
    candidates = db.query(ExtractionJob.id).filter_by(status='waiting_access', cancel_requested=False)
    if job_ids is not None:
        candidates = candidates.filter(ExtractionJob.id.in_(job_ids))
    candidate_ids = [row.id for row in candidates]
    if not candidate_ids:
        return exhausted
    db.rollback()
    _begin_queue_write(db)
    exhausted = {store for store in exhausted if transport_retries_exhausted(store)}
    for job in db.query(ExtractionJob).filter(
        ExtractionJob.id.in_(candidate_ids), ExtractionJob.status == 'waiting_access',
        ExtractionJob.cancel_requested.is_(False),
    ).with_for_update():
        if any(item.status in {'queued', 'running'} for item in job.items):
            continue
        waiting = [item for item in job.items if item.status == 'waiting_access']
        pending = {store for item in waiting for store in json.loads(item.waiting_stores_json or '[]')}
        if not pending or not pending <= exhausted:
            continue
        names = ', '.join(store['label'] for store in TARGET_STORES if store['key'] in pending)
        now = datetime.utcnow()
        message = f'{names} não respondeu às tentativas automáticas. As ofertas já coletadas foram preservadas.'
        for item in waiting:
            item.status = 'failed'
            item.completed_at = now
            item.message = message
        job.status = 'completed_with_errors'
        job.completed_at = job.updated_at = now
        job.current_sku = None
        job.processed_skus = min(job.total_skus, (job.processed_skus or 0) + len(waiting))
        job.error_count = (job.error_count or 0) + len(waiting)
        job.message = message
        _record_schedule_completion(db, job, now)
    db.commit()
    return exhausted


def _finalize_cancelled_job(db, job, completed_at):
    db.query(ExtractionJobItem).filter(
        ExtractionJobItem.job_id == job.id,
        ExtractionJobItem.status.in_(("queued", "running", "waiting_access")),
    ).update({
        ExtractionJobItem.status: "cancelled",
        ExtractionJobItem.waiting_stores_json: None,
        ExtractionJobItem.resume_stores_json: None,
        ExtractionJobItem.message: "Processamento interrompido pelo cancelamento.",
        ExtractionJobItem.completed_at: completed_at,
    }, synchronize_session=False)
    job.status = "cancelled"
    job.completed_at = job.updated_at = completed_at
    job.current_sku = None
    job.message = f"Fila cancelada após {job.processed_skus} de {job.total_skus} SKUs."
    _record_schedule_completion(db, job, completed_at)


def _finish_or_requeue_job(job_id, cancellation=None):
    db = SessionLocal()
    try:
        _begin_queue_write(db)
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).with_for_update().one()
        if job.cancel_requested or (cancellation is not None and cancellation.is_set()):
            if job.status != "cancelled":
                job.cancel_requested = True
                _finalize_cancelled_job(db, job, datetime.utcnow())
                db.commit()
            return "cancelled"
        queued_items = (
            db.query(ExtractionJobItem)
            .filter(
                ExtractionJobItem.job_id == job_id,
                ExtractionJobItem.status == "queued",
            )
            .all()
        )
        now = datetime.utcnow()
        if job.status=='paused':
            job.current_sku=None
            job.message='Extração pausada. Os dados e checkpoints foram preservados.'
        elif not queued_items and any(item.status=='waiting_access' for item in job.items):
            job.status='waiting_access';job.current_sku=None
            job.message='As ofertas coletadas estão salvas. Há lojas pendentes.'
        elif not queued_items:
            job.status = "completed_with_errors" if job.error_count else "completed"
            job.completed_at = now
            job.current_sku = None
            job.message = (
                f"{job.processed_skus} SKUs processados; "
                f"{job.success_count} concluidos e {job.error_count} com erro."
            )
            _record_schedule_completion(db, job, now)
        else:
            job.status = "queued"
            job.current_sku = None
            job.message = (
                f"Lote concluido. {job.processed_skus} de {job.total_skus} SKUs processados."
            )
        job.updated_at = now
        db.commit()
        return job.status
    finally:
        db.close()


def _run_item(job_id, item_id):
    options = _mark_item_running(job_id, item_id)
    if not options:
        return None

    result = None
    try:
        for attempt in range(MAX_ITEM_ATTEMPTS):
            check_cancelled()
            try:
                result = _processor(options["sku"], options)
            except Exception as exc:
                result = {
                    "status": "error",
                    "message": str(exc) or exc.__class__.__name__,
                }
            if result.get("status") == "error":
                check_cancelled()
            if result.get("status") != "error" or attempt + 1 >= MAX_ITEM_ATTEMPTS:
                break
            interruptible_sleep(min(4.0, 1.5 * (attempt + 1)))
    except ExtractionCancelled:
        return None

    return _finish_item(job_id, item_id, result or {"status": "error", "message": "Falha desconhecida."})


def _process_progressive_batch(job_id, process_slice, workers, *, serialize_marketplaces):
    """Schedule both phases while keeping browser-backed SKUs from competing."""
    handled = {'general': set(), 'marketplaces': set()}
    active = {}
    deadline = time.monotonic() + PRECISE_SKU_SLICE_SECONDS

    def candidates(phase, limit):
        in_flight = {item_id for item_id, _ in active.values()}
        return _job_item_ids(job_id, exclude=handled[phase] | in_flight, phase=phase, limit=limit)[0]

    def submit(pool, ids, phase, capacity):
        scheduled = ids[:capacity]
        for item_id in scheduled:
            active[pool.submit(process_slice, item_id)] = (item_id, phase)
            handled[phase].add(item_id)
        return len(scheduled)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='precise-io') as pool:
        while True:
            if time.monotonic() < deadline:
                with SessionLocal() as db:
                    capacity = max(0, min(workers, _sku_workers(db, True)) - len(active))
                if capacity:
                    general = candidates('general', capacity)
                    general_active = any(phase == 'general' for _, phase in active.values())
                    general_pending = general_active or bool(general)
                    marketplace_active = sum(phase == 'marketplaces' for _, phase in active.values())
                    marketplace_capacity = capacity
                    if general_pending or serialize_marketplaces:
                        marketplace_capacity = min(capacity, max(0, 1 - marketplace_active))
                    if marketplace_capacity and (general_active or capacity > 1 or not general):
                        market = candidates('marketplaces', marketplace_capacity)
                        capacity -= submit(pool, market, 'marketplaces', marketplace_capacity)
                    submit(pool, general, 'general', capacity)
            if not active:
                break
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                active.pop(future)
                future.result()


def _process_job_batch(job_id):
    event = threading.Event()
    with _batch_events_lock:
        _batch_events[job_id] = event
    try:
        return _process_job_batch_with_cancellation(job_id, event)
    finally:
        with _batch_events_lock:
            _batch_events.pop(job_id, None)


def _process_job_batch_with_cancellation(job_id, event):
    item_ids, precise = _job_item_ids(job_id)
    if not item_ids:
        return _finish_or_requeue_job(job_id, event)

    def process_slice(item_id):
        started = time.monotonic()
        rounds = 0
        status = None
        while not event.is_set():
            with cancellation_scope(event):
                status = _run_item(job_id, item_id)
            if status is None:
                break
            rounds += 1
            with SessionLocal() as phase_db:
                phase_changed = phase_db.query(ExtractionJobItem.store_phase).filter_by(id=item_id).scalar() == 'marketplaces'
            if (
                not precise or status != "partial" or phase_changed
                or rounds >= PRECISE_SKU_SLICE_ROUNDS
                or time.monotonic() - started >= PRECISE_SKU_SLICE_SECONDS
            ):
                break
        return status

    from services.extraction_resources import settings
    with SessionLocal() as control_db:
        workers = _sku_workers(control_db, precise)
        allow_early = False
        serialize_marketplaces = False
        refill_phase = None
        if precise and DEFER_MARKETPLACES and workers > 1:
            job = control_db.get(ExtractionJob, job_id)
            stores = set(_requested_stores(job))
            mixed = bool(stores & LAST_STORES and stores - LAST_STORES)
            allow_early = mixed and settings()['hardware_acceleration']
            if 'shopee' in stores:
                if allow_early:
                    serialize_marketplaces = True
                elif mixed and control_db.query(ExtractionJobItem.id).filter_by(
                    job_id=job_id, store_phase='general', status='queued',
                ).first() is not None:
                    refill_phase = 'general'
                else:
                    workers = 1
    if allow_early:
        _process_progressive_batch(job_id, process_slice, workers,
                                   serialize_marketplaces=serialize_marketplaces)
    elif workers == 1:
        for index, item_id in enumerate(item_ids):
            process_slice(item_id)
            if event.is_set():
                break
            if not precise and index + 1 < len(item_ids) and event.wait(SKU_PAUSE_SECONDS):
                break
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='sku-io') as pool:
            futures = {pool.submit(process_slice, item_id): item_id for item_id in item_ids}
            handled = set(item_ids)
            deadline = time.monotonic() + PRECISE_SKU_SLICE_SECONDS
            while futures:
                completed, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    futures.pop(future)
                    future.result()
                if event.is_set() or len(futures) >= workers or time.monotonic() >= deadline:
                    continue
                # Refill immediately: one slow SKU must not leave other slots idle.
                candidates, _ = _job_item_ids(job_id, exclude=handled, phase=refill_phase)
                with SessionLocal() as control_db:
                    available = max(0, min(workers, _sku_workers(control_db, precise))-len(futures))
                for item_id in candidates[:available]:
                    futures[pool.submit(process_slice, item_id)] = item_id
                    handled.add(item_id)

    return _finish_or_requeue_job(job_id, event)


def _requeue_job_after_worker_error(job_id, exc):
    db = SessionLocal()
    try:
        _begin_queue_write(db)
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).with_for_update().first()
        if not job or job.status in TERMINAL_STATUSES:
            return
        if job.cancel_requested:
            _finalize_cancelled_job(db, job, datetime.utcnow())
            db.commit()
            return
        running_items = (
            db.query(ExtractionJobItem)
            .filter(
                ExtractionJobItem.job_id == job_id,
                ExtractionJobItem.status == "running",
            )
            .all()
        )
        for item in running_items:
            item.status = "queued"
            item.started_at = None
        job.status = "queued" if job.status!='paused' else 'paused'
        job.current_sku = None
        job.message = f"Retomando fila apos falha interna: {str(exc)[:300]}"
        job.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _worker_loop():
    while True:
        if _processor is None:
            _wake_event.wait(timeout=1.0)
            _wake_event.clear()
            continue

        try:
            job_id = _next_job_id()
        except OperationalError as exc:
            print(f" WARNING: Nao foi possivel consultar a fila: {exc}")
            _wake_event.wait(timeout=2.0)
            _wake_event.clear()
            continue
        if not job_id:
            _wake_event.wait(timeout=5.0)
            _wake_event.clear()
            continue

        try:
            status = _process_job_batch(job_id)
            if status == "queued":
                with SessionLocal() as pause_db:
                    precise = pause_db.query(ExtractionJob.mode).filter(ExtractionJob.id == job_id).scalar() == 'precise'
                _wake_event.wait(timeout=PRECISE_BATCH_PAUSE_SECONDS if precise else BATCH_PAUSE_SECONDS)
                _wake_event.clear()
        except Exception as exc:
            print(f" WARNING: Fila de extracao falhou no job {job_id}: {exc}")
            try:
                _requeue_job_after_worker_error(job_id, exc)
            except Exception as recovery_exc:
                print(f" WARNING: Nao foi possivel recuperar o job {job_id}: {recovery_exc}")
            time.sleep(2.0)


def start_extraction_queue():
    global _started
    with _start_lock:
        if _started:
            return
        _recover_interrupted_jobs()
        _cleanup_old_jobs()
        _started = True
        thread = threading.Thread(
            target=_worker_loop,
            name="nevetracker-extraction-queue",
            daemon=True,
        )
        thread.start()
        _wake_event.set()
