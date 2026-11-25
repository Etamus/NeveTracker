"""Consistent SQLite snapshots and transactional, schema-checked restoration."""
from datetime import datetime
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from fastapi import HTTPException


def snapshot(engine, destination):
    if engine.dialect.name != 'sqlite':
        raise HTTPException(400, 'A migração por arquivo exige o banco SQLite local.')
    with engine.connect() as connection, closing(sqlite3.connect(str(destination))) as output:
        connection.connection.driver_connection.backup(output, pages=1024)


def restore(engine, filename, backup_dir):
    from services.extraction_queue import _enqueue_lock, ACTIVE_STATUSES, _wake_event
    if engine.dialect.name != 'sqlite':
        raise HTTPException(400, 'A migração por arquivo exige o banco SQLite local.')
    path = Path(filename).resolve()
    try:
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        source.execute('PRAGMA trusted_schema=OFF')
        if source.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('O arquivo está corrompido.')
        if source.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:
            raise ValueError('O arquivo contém estruturas incompatíveis.')
        with _enqueue_lock, engine.connect() as connection:
            target = connection.connection.driver_connection
            tables = sorted(row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'auth_users', 'known_skus', 'products', 'price_history', 'extraction_jobs'}.issubset(source_tables):
                raise ValueError('Selecione um banco completo exportado pelo NeveTracker.')
            columns = {}
            for table in tables:
                # Only names from our own schema are interpolated, never SQL from the upload.
                expected = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
                actual = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                if table == 'crawler_control' and 'hardware_acceleration' not in actual:
                    expected = [column for column in expected if column != 'hardware_acceleration']
                if set(actual) != set(expected):
                    raise ValueError('Versão do banco incompatível. Atualize o projeto nos dois computadores antes de migrar.')
                columns[table] = expected
            placeholders = ','.join('?' for _ in ACTIVE_STATUSES)
            if target.execute(f'SELECT 1 FROM extraction_jobs WHERE status IN ({placeholders}) LIMIT 1', ACTIVE_STATUSES).fetchone():
                raise HTTPException(409, 'Conclua ou cancele a extração antes de importar o banco.')
            if target.execute("SELECT 1 FROM lead_time_quotes WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise HTTPException(409, 'Aguarde as consultas de frete e prazo antes de importar o banco.')
            if not source.execute("SELECT 1 FROM auth_users WHERE role IN ('developer','administrator') OR is_owner=1 LIMIT 1").fetchone():
                raise ValueError('O banco não possui uma conta de administrador válida.')
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f'antes-importacao-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
            with closing(sqlite3.connect(str(backup))) as output:
                target.backup(output, pages=1024)
            try:
                target.execute('BEGIN IMMEDIATE')
                if target.execute(f'SELECT 1 FROM extraction_jobs WHERE status IN ({placeholders}) LIMIT 1', ACTIVE_STATUSES).fetchone() or target.execute("SELECT 1 FROM lead_time_quotes WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise HTTPException(409, 'Uma coleta foi iniciada. Aguarde a conclusão antes de importar.')
                target.execute('PRAGMA defer_foreign_keys=ON')
                for table in reversed(tables):
                    target.execute(f'DELETE FROM "{table}"')
                counts = {}
                for table in tables:
                    # Browser sessions are machine-specific. Users/passwords migrate,
                    # while bearer tokens must be issued again on the destination.
                    if table == 'auth_sessions':
                        continue
                    names = ','.join(f'"{column}"' for column in columns[table])
                    cursor = source.execute(f'SELECT {names} FROM "{table}"')
                    insert = f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns[table])})'
                    counts[table] = 0
                    while rows := cursor.fetchmany(1000):
                        target.executemany(insert, rows)
                        counts[table] += len(rows)
                target.execute("UPDATE extraction_jobs SET status='queued', current_sku=NULL WHERE status='running'")
                target.execute("UPDATE extraction_job_items SET status='queued' WHERE status='running'")
                if target.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('O arquivo possui referências inconsistentes; o banco atual foi preservado.')
                target.commit()
            except Exception:
                target.rollback()
                raise
        _wake_event.set()
        return {'tables': counts, 'relogin_required': True}
    except (sqlite3.Error, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if 'source' in locals():
            source.close()


def temporary_snapshot(engine):
    handle, filename = tempfile.mkstemp(suffix='.db', prefix='nevetracker-')
    import os
    os.close(handle)
    try:
        snapshot(engine, filename)
        return filename
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise
