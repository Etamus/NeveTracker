import threading
import time
from datetime import datetime, timedelta, timezone
from database import SessionLocal
from models.product import CrawlerControl, ExtractionSchedule, ExtractionJob, KnownSku
from services.extraction_queue import enqueue_extraction_job


LOCAL_TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
SCHEDULER_INTERVAL_SECONDS = 30

_started = False
_start_lock = threading.Lock()


def _clock_minutes(value, fallback):
    try:
        parsed = datetime.strptime(value or fallback, "%H:%M")
    except (TypeError, ValueError):
        parsed = datetime.strptime(fallback, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _schedule_days(value):
    days = set()
    for item in (value or "1").split(","):
        try:
            day = int(item)
        except ValueError:
            continue
        if 0 <= day <= 6:
            days.add(day)
    return days or {1}


def _schedule_snapshot():
    db = SessionLocal()
    try:
        schedule = db.query(ExtractionSchedule).filter(ExtractionSchedule.id == 1).first()
        if not schedule:
            return None
        from models.auth import AuthUser
        from services.auth_service import user_role
        owner = db.get(AuthUser, schedule.owner_user_id) if schedule.owner_user_id else None
        allowed = not schedule.owner_user_id or (owner and user_role(owner) != 'viewer')
        return {
            "enabled": bool(schedule.enabled and allowed),
            "owner_user_id": schedule.owner_user_id,
            "api_with_scraping": db.query(CrawlerControl.api_with_scraping).filter_by(id=1).scalar() is not False,
            "postal_code": schedule.postal_code or '01001000',
            "start_time": schedule.start_time,
            "end_time": schedule.end_time,
            "days": _schedule_days(schedule.days),
            "repeat_every_hours": min(12, max(0, schedule.repeat_every_hours or 0)),
            "force_refresh": schedule.force_refresh,
            "only_stale": schedule.only_stale,
            "last_run_key": schedule.last_run_key,
            "mode": (
                db.query(CrawlerControl.default_mode)
                .filter(CrawlerControl.id == 1)
                .scalar()
                or "quick"
            ),
        }
    finally:
        db.close()


def _due_run_key(schedule, now_utc=None):
    if not schedule or not schedule.get("enabled"):
        return None

    now_utc = now_utc or datetime.utcnow()
    local_now = now_utc.replace(tzinfo=timezone.utc).astimezone(LOCAL_TIMEZONE)
    weekday = local_now.isoweekday() % 7
    if weekday not in schedule.get("days", {1}):
        return None

    start_minutes = _clock_minutes(schedule.get("start_time"), "09:00")
    end_minutes = max(start_minutes, _clock_minutes(schedule.get("end_time"), "18:00"))
    now_minutes = local_now.hour * 60 + local_now.minute
    if now_minutes < start_minutes or now_minutes > end_minutes:
        return None

    repeat_hours = min(12, max(0, int(schedule.get("repeat_every_hours") or 0)))
    if repeat_hours == 0:
        slot_minutes = start_minutes
    else:
        interval_minutes = repeat_hours * 60
        slot_minutes = start_minutes + ((now_minutes - start_minutes) // interval_minutes) * interval_minutes
        if slot_minutes > end_minutes:
            return None

    return f"{local_now.date().isoformat()}-{slot_minutes // 60:02d}{slot_minutes % 60:02d}"


def _known_skus():
    db = SessionLocal()
    try:
        skus = [
            row[0]
            for row in db.query(KnownSku.sku).order_by(KnownSku.sku.asc()).all()
            if row[0]
        ]
        return skus
    finally:
        db.close()


def _run_cycle(postal_code, run_key, force_refresh, mode="quick", only_stale=False, owner_user_id=None, api_with_scraping=True):
    with SessionLocal() as db:
        active = db.query(ExtractionJob).filter(
            ExtractionJob.schedule_run_key == run_key,
            ExtractionJob.status.in_(("queued", "running", "waiting_access", "paused")),
        ).first()
        if active:
            return None
    skus = _known_skus()
    if not skus:
        # Record a completed evaluation so an empty slot is not retried
        # every 30 seconds.
        with SessionLocal() as db:
            schedule = db.query(ExtractionSchedule).filter_by(id=1).first()
            if schedule:
                schedule.last_run_key = run_key
                schedule.last_run = datetime.utcnow()
                db.commit()
        return None
    job = enqueue_extraction_job(
        skus,
        kind="scheduled",
        force_refresh=True if only_stale else force_refresh,
        refresh_if_stale=only_stale,
        postal_code=postal_code,
        scope="all",
        mode=mode,
        schedule_run_key=run_key,
        owner_user_id=owner_user_id,
        api_with_scraping=api_with_scraping,
    )
    print(
        f" LOG: Agendamento enfileirado para {len(skus)} SKUs "
        f"no job {job['id']}."
    )
    return job


def _tick():
    schedule = _schedule_snapshot()
    run_key = _due_run_key(schedule)
    if not run_key or schedule.get("last_run_key") == run_key:
        return
    _run_cycle(
        schedule.get("postal_code"),
        run_key,
        schedule.get("force_refresh", True),
        schedule.get("mode", "quick"),
        schedule.get("only_stale", False),
        schedule.get('owner_user_id'),
        schedule.get('api_with_scraping', True),
    )


def _scheduler_loop():
    while True:
        try:
            _tick()
        except Exception as exc:
            print(f" WARNING: Ciclo do agendador falhou: {exc}")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start_extraction_scheduler():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(
            target=_scheduler_loop,
            name="nevetracker-extraction-scheduler",
            daemon=True,
        )
        thread.start()
