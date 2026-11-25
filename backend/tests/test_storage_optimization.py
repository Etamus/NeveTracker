import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session

from database import Base
from models.product import ExtractionJob,ExtractionJobItem
from storage_codec import pack_text,unpack_text,PREFIX
from optimize_database import optimize_database
from services.database_maintenance import maintain_idle_database


class StorageOptimizationTests(unittest.TestCase):
    def test_codec_preserves_unicode_whitespace_and_small_plaintext(self):
        for value in (None,'{}','[]',json.dumps({'sku':'W10531315','descrição':'Peças e refrigeração 🧊'*200},ensure_ascii=False,indent=2)):
            packed=pack_text(value)
            self.assertEqual(unpack_text(packed),value)
            self.assertEqual(pack_text(packed),packed)
        large=json.dumps({'repeated':'value'*2000})
        self.assertTrue(pack_text(large).startswith(PREFIX))
        self.assertLess(len(pack_text(large)),len(large)/4)

    def test_bad_packed_values_fail_explicitly_without_returning_partial_json(self):
        for value in (PREFIX+'bad!',PREFIX+'',pack_text('x'*2000)+'AAAA'):
            with self.assertRaises(ValueError): unpack_text(value)

    def test_orm_reads_legacy_json_and_compacts_new_checkpoints(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        payload=json.dumps([{'sku':'W10531315','value':'sensor'*500}]*10)
        with Session(engine) as db:
            job=ExtractionJob(id='j',request_key='j',status='completed')
            job.items=[ExtractionJobItem(sku='W10531315',position=0,results_json=payload)]
            db.add(job);db.commit();item_id=job.items[0].id
            stored=db.execute(text('SELECT results_json FROM extraction_job_items WHERE id=:id'),{'id':item_id}).scalar_one()
            self.assertTrue(stored.startswith(PREFIX))
            db.expire_all()
            self.assertEqual(db.get(ExtractionJobItem,item_id).results_json,payload)
            db.execute(text('UPDATE extraction_job_items SET results_json=:raw WHERE id=:id'),{'raw':payload,'id':item_id});db.commit();db.expire_all()
            self.assertEqual(db.get(ExtractionJobItem,item_id).results_json,payload)

    def test_offline_migration_keeps_full_backup_and_all_logical_values(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'source.db';engine=create_engine('sqlite:///'+path.as_posix())
            Base.metadata.create_all(engine)
            payload=json.dumps({'sku':'W10531315','details':'repeated data '*1000})
            with Session(engine) as db:
                job=ExtractionJob(id='j',request_key='j',status='completed')
                job.items=[ExtractionJobItem(sku='W10531315',position=0,results_json='[]')]
                db.add(job);db.commit()
                db.execute(text('UPDATE extraction_job_items SET results_json=:value'),{'value':payload});db.commit()
            engine.dispose()
            result=optimize_database(path,Path(folder)/'backups')
            self.assertTrue(result['all_values_preserved'])
            self.assertTrue(Path(result['backup']).is_file())
            with closing(sqlite3.connect(result['backup'])) as before,closing(sqlite3.connect(path)) as after:
                self.assertEqual(before.execute('SELECT results_json FROM extraction_job_items').fetchone()[0],payload)
                stored=after.execute('SELECT results_json FROM extraction_job_items').fetchone()[0]
                self.assertTrue(stored.startswith(PREFIX));self.assertEqual(unpack_text(stored),payload)
                self.assertEqual(after.execute('PRAGMA auto_vacuum').fetchone()[0],2)

    def test_maintenance_and_migration_skip_pending_extractions(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'active.db';engine=create_engine('sqlite:///'+path.as_posix())
            Base.metadata.create_all(engine)
            try:
                with Session(engine) as db:
                    db.add(ExtractionJob(id='active',request_key='active',status='running'));db.commit()
                self.assertEqual(maintain_idle_database(engine),{'skipped':'active collection'})
                with self.assertRaises(ValueError): optimize_database(path,Path(folder)/'backups')
                with Session(engine) as db:
                    job=db.get(ExtractionJob,'active');self.assertEqual(job.status,'running')
                    job.status='completed';db.commit()
                self.assertIn('pages_reclaimed',maintain_idle_database(engine))
            finally: engine.dispose()


if __name__=='__main__':unittest.main()
