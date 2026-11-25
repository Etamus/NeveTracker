"""Offline, audited SQLite compaction. Keeps a complete pre-change backup."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from storage_codec import pack_text, unpack_text

PACKED_COLUMNS = {'lead_time_quotes':'item_json','offer_observations':'snapshot_json','extraction_job_items':'results_json'}


def identifier(value):
    return '"' + value.replace('"','""') + '"'


def audit(connection):
    result={}
    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        columns=connection.execute(f'PRAGMA table_info({identifier(table)})').fetchall()
        names=[row[1] for row in columns]
        keys=[row[1] for row in sorted(columns,key=lambda row:row[5]) if row[5]]
        order=','.join(identifier(key) for key in keys) or 'rowid'
        digest=hashlib.sha256();count=0
        packed=names.index(PACKED_COLUMNS[table]) if table in PACKED_COLUMNS else None
        for record in connection.execute(f'SELECT * FROM {identifier(table)} ORDER BY {order}'):
            values=list(record)
            if packed is not None: values[packed]=unpack_text(values[packed])
            digest.update(json.dumps(values,ensure_ascii=True,separators=(',',':'),default=str).encode('utf-8'))
            digest.update(b'\n');count+=1
        result[table]={'rows':count,'sha256':digest.hexdigest()}
    return result


def optimize_database(filename, backup_dir):
    path=Path(filename).resolve()
    if not path.is_file(): raise ValueError('Banco não encontrado.')
    backup_dir=Path(backup_dir).resolve();backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/f'pre-optimization-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.db'
    started=time.perf_counter()
    with closing(sqlite3.connect(str(path),timeout=3,isolation_level=None)) as connection:
        connection.execute('PRAGMA locking_mode=EXCLUSIVE')
        connection.execute('BEGIN EXCLUSIVE')
        if connection.execute("SELECT 1 FROM extraction_jobs WHERE status IN ('queued','running','paused','waiting_access') LIMIT 1").fetchone():
            connection.rollback()
            raise ValueError('Há uma extração pendente. Aguarde sua conclusão antes de otimizar.')
        connection.commit()
        if connection.execute('PRAGMA quick_check').fetchone()!=('ok',): raise ValueError('O banco não passou na verificação de integridade.')
        before_size=path.stat().st_size
        print('Criando cópia de segurança...',flush=True)
        with closing(sqlite3.connect(str(backup))) as target: connection.backup(target,pages=1024)
        print('Auditando os valores originais...',flush=True)
        before=audit(connection)
        changed={}
        for table,field in PACKED_COLUMNS.items():
            last_id=0;count=0;scanned=0;reported=0
            while True:
                rows=connection.execute(f'SELECT id,{identifier(field)} FROM {identifier(table)} WHERE id>? ORDER BY id LIMIT 100',(last_id,)).fetchall()
                if not rows: break
                updates=[]
                for row_id,raw in rows:
                    packed=pack_text(raw)
                    if packed!=raw:
                        if unpack_text(packed)!=unpack_text(raw): raise ValueError('A compactação não preservou o conteúdo.')
                        updates.append((packed,row_id))
                    last_id=row_id
                if updates:
                    connection.execute('BEGIN IMMEDIATE')
                    try:
                        connection.executemany(f'UPDATE {identifier(table)} SET {identifier(field)}=? WHERE id=?',updates)
                        connection.commit()
                    except Exception:
                        connection.rollback();raise
                    count+=len(updates)
                scanned+=len(rows)
                if scanned//10000>reported:
                    reported=scanned//10000
                    print(f'{table}: {scanned} registros conferidos',flush=True)
            changed[table]=count
        connection.execute('CREATE INDEX IF NOT EXISTS ix_observation_sku_date ON offer_observations(sku, observed_at DESC, id DESC)')
        connection.execute('CREATE INDEX IF NOT EXISTS ix_source_offer_validation ON product_sources(sku, url, seller, validation_status)')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
        print('Reorganizando páginas do banco...',flush=True)
        connection.execute('VACUUM')
        connection.execute('PRAGMA analysis_limit=1000')
        connection.execute('PRAGMA optimize')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        print('Conferindo todos os valores após a compactação...',flush=True)
        after=audit(connection)
        if before!=after: raise ValueError(f'A auditoria detectou divergência. Cópia original: {backup}')
        if connection.execute('PRAGMA integrity_check').fetchone()!=('ok',): raise ValueError('Falha de integridade após compactação.')
        result={'database':str(path),'backup':str(backup),'before_bytes':before_size,'after_bytes':path.stat().st_size,
                'seconds':round(time.perf_counter()-started,2),'packed_records':changed,'audit':after,'all_values_preserved':True}
        print(json.dumps({key:value for key,value in result.items() if key!='audit'},ensure_ascii=False),flush=True)
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Otimizar o SQLite com o backend fechado, preservando todos os registros.')
    parser.add_argument('--database',default=str(Path(__file__).with_name('precos.db')))
    parser.add_argument('--backup-dir',default=str(Path(__file__).resolve().parent.parent/'.backups'))
    parser.add_argument('--report')
    args=parser.parse_args()
    result=optimize_database(args.database,args.backup_dir)
    if args.report: Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
