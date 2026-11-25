"""Bounded SQLite housekeeping while collection is idle; never deletes records."""
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()


def maintain_idle_database(engine):
    if engine.dialect.name != 'sqlite' or not engine.url.database or engine.url.database == ':memory:':
        return {'skipped': 'non-file database'}
    path = Path(engine.url.database)
    if not path.is_file():
        return {'skipped': 'missing database'}
    from services.extraction_queue import _enqueue_lock
    with _enqueue_lock, closing(sqlite3.connect(str(path), timeout=1, isolation_level=None)) as connection:
        try:
            connection.execute('BEGIN IMMEDIATE')
            active = connection.execute("SELECT 1 FROM extraction_jobs WHERE status IN ('queued','running','waiting_access','paused') LIMIT 1").fetchone()
            pending = connection.execute("SELECT 1 FROM lead_time_quotes WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active or pending:
                connection.rollback()
                return {'skipped': 'active collection'}
            before = connection.execute('PRAGMA page_count').fetchone()[0]
            if connection.execute('PRAGMA auto_vacuum').fetchone()[0] == 2:
                connection.execute('PRAGMA incremental_vacuum(256)')
            connection.commit()
            connection.execute('PRAGMA analysis_limit=1000')
            connection.execute('PRAGMA optimize')
            checkpoint = connection.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            wal = Path(str(path) + '-wal')
            if checkpoint[0] == 0 and checkpoint[1] == checkpoint[2] and wal.exists() and wal.stat().st_size > 64 * 1024**2:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            after = connection.execute('PRAGMA page_count').fetchone()[0]
            return {'pages_reclaimed': max(0,before-after)}
        finally:
            if connection.in_transaction:
                connection.rollback()


def start_database_maintenance(engine):
    global _started
    with _start_lock:
        if _started or engine.dialect.name != 'sqlite':
            return
        _started = True
    def run():
        if _stop.wait(60): return
        while not _stop.is_set():
            try:
                maintain_idle_database(engine)
            except (sqlite3.Error, OSError):
                logging.getLogger(__name__).exception('Manutenção do banco adiada.')
            if _stop.wait(3600): return
    threading.Thread(target=run,name='database-maintenance',daemon=True).start()
