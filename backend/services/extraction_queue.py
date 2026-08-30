from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from database import SessionLocal
from models.product import ExtractionJob, ExtractionJobItem, ExtractionSchedule


BATCH_SIZE = 30
MAX_JOB_SKUS = 5000
MAX_ITEM_ATTEMPTS = 2
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "completed_with_errors", "cancelled", "failed")
JOB_RETENTION_DAYS = 30
MAX_RETAINED_JOBS = 200


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

_processor = None
_started = False
_start_lock = threading.Lock()
_wake_event = threading.Event()


def configure_extraction_processor(processor):
    global _processor
    _processor = processor


def _request_key(skus, kind, force_refresh, refresh_if_stale, postal_code, scope, run_key):
    parts = [
        kind,
        str(bool(force_refresh)),
        str(bool(refresh_if_stale)),
        postal_code or "",
        scope,
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


def serialize_job(job, include_items=False):
    response = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "scope": job.scope,
        "force_refresh": job.force_refresh,
        "refresh_if_stale": job.refresh_if_stale,
        "batch_size": job.batch_size,
        "total_skus": job.total_skus,
        "processed_skus": job.processed_skus,
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
    schedule_run_key=None,
):
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
        schedule_run_key,
    )
    db = SessionLocal()
    try:
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
            kind=kind,
            status="queued",
            priority=_priority_for_kind(kind),
            request_key=request_key,
            scope=scope,
            force_refresh=bool(force_refresh),
            refresh_if_stale=bool(refresh_if_stale),
            postal_code=postal_code,
            schedule_run_key=schedule_run_key,
            batch_size=BATCH_SIZE,
            total_skus=len(normalized_skus),
            message=f"{len(normalized_skus)} SKUs aguardando processamento.",
        )
        job.items = [
            ExtractionJobItem(sku=sku, position=index)
            for index, sku in enumerate(normalized_skus)
        ]
        db.add(job)
        db.commit()
        db.refresh(job)
        response = serialize_job(job)
    finally:
        db.close()

    _wake_event.set()
    return response


def get_extraction_job(job_id, include_items=False):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).first()
        return serialize_job(job, include_items=include_items) if job else None
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


def cancel_extraction_job(job_id):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).first()
        if not job:
            return None
        if job.status not in TERMINAL_STATUSES:
            job.cancel_requested = True
            job.message = "Cancelamento solicitado. Finalizando o SKU atual."
            job.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(job)
            _wake_event.set()
        return serialize_job(job)
    finally:
        db.close()


def _recover_interrupted_jobs():
    db = SessionLocal()
    try:
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
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).one()
        item = db.query(ExtractionJobItem).filter(ExtractionJobItem.id == item_id).one()
        if job.cancel_requested:
            return None
        now = datetime.utcnow()
        job.status = "running"
        job.started_at = job.started_at or now
        job.current_sku = item.sku
        job.message = f"Processando {item.sku}."
        item.status = "running"
        item.attempts = (item.attempts or 0) + 1
        item.started_at = now
        db.commit()
        return {
            "sku": item.sku,
            "scope": job.scope,
            "force_refresh": job.force_refresh,
            "refresh_if_stale": job.refresh_if_stale,
            "postal_code": job.postal_code,
            "attempt": item.attempts,
            "job_created_at": job.created_at,
        }
    finally:
        db.close()


def _finish_item(job_id, item_id, result):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).one()
        item = db.query(ExtractionJobItem).filter(ExtractionJobItem.id == item_id).one()
        result_status = result.get("status") or "error"
        if result_status == "error":
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
        item.completed_at = datetime.utcnow()
        job.processed_skus = (job.processed_skus or 0) + 1
        job.current_sku = None
        job.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _job_item_ids(job_id):
    db = SessionLocal()
    try:
        return [
            row[0]
            for row in (
                db.query(ExtractionJobItem.id)
                .filter(
                    ExtractionJobItem.job_id == job_id,
                    ExtractionJobItem.status == "queued",
                )
                .order_by(ExtractionJobItem.position.asc())
                .limit(BATCH_SIZE)
                .all()
            )
        ]
    finally:
        db.close()


def _record_schedule_completion(db, job, completed_at):
    if not job.schedule_run_key:
        return
    schedule = db.query(ExtractionSchedule).filter(ExtractionSchedule.id == 1).first()
    if schedule:
        schedule.last_run_key = job.schedule_run_key
        schedule.last_run = completed_at


def _finish_or_requeue_job(job_id):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).one()
        queued_items = (
            db.query(ExtractionJobItem)
            .filter(
                ExtractionJobItem.job_id == job_id,
                ExtractionJobItem.status == "queued",
            )
            .all()
        )
        now = datetime.utcnow()
        if job.cancel_requested:
            for item in queued_items:
                item.status = "cancelled"
                item.message = "Cancelado antes do processamento."
                item.completed_at = now
            job.status = "cancelled"
            job.completed_at = now
            job.current_sku = None
            job.message = f"Fila cancelada apos {job.processed_skus} de {job.total_skus} SKUs."
            _record_schedule_completion(db, job, now)
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
        return False

    result = None
    for attempt in range(MAX_ITEM_ATTEMPTS):
        try:
            result = _processor(options["sku"], options)
        except Exception as exc:
            result = {
                "status": "error",
                "message": str(exc) or exc.__class__.__name__,
            }
        if result.get("status") != "error" or attempt + 1 >= MAX_ITEM_ATTEMPTS:
            break
        time.sleep(min(4.0, 1.5 * (attempt + 1)))

    _finish_item(job_id, item_id, result or {"status": "error", "message": "Falha desconhecida."})
    return True


def _process_job_batch(job_id):
    item_ids = _job_item_ids(job_id)
    if not item_ids:
        return _finish_or_requeue_job(job_id)

    for index, item_id in enumerate(item_ids):
        if not _run_item(job_id, item_id):
            break
        if index + 1 < len(item_ids):
            time.sleep(SKU_PAUSE_SECONDS)

    return _finish_or_requeue_job(job_id)


def _requeue_job_after_worker_error(job_id, exc):
    db = SessionLocal()
    try:
        job = db.query(ExtractionJob).filter(ExtractionJob.id == job_id).first()
        if not job or job.status in TERMINAL_STATUSES:
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
        job.status = "queued"
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

        job_id = _next_job_id()
        if not job_id:
            _wake_event.wait(timeout=5.0)
            _wake_event.clear()
            continue

        try:
            status = _process_job_batch(job_id)
            if status == "queued":
                _wake_event.wait(timeout=BATCH_PAUSE_SECONDS)
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
