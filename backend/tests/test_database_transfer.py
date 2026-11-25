from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from database import Base
from models.auth import AuthUser, AuthSession
from models.product import KnownSku, Product, PriceHistory, ExtractionJob
from services.database_transfer import snapshot, restore


class DatabaseTransferTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)
        self.engines=[]
        for name,sku in [('source','W10531315'),('target','W11199342')]:
            engine=create_engine(f'sqlite:///{self.path / (name+".db")}')
            Base.metadata.create_all(engine);self.engines.append(engine);self.addCleanup(engine.dispose)
            with Session(engine) as db:
                db.add(AuthUser(id=1,username='DEUSM2',password_hash='hash',password_salt='salt',is_owner=True,role='developer'))
                db.add(KnownSku(sku=sku));db.add(Product(id=1,sku=sku,name='Sensor '+sku))
                db.commit();db.add(PriceHistory(product_id=1,price=42,store='Dufrio',source='Dufrio',url='https://test.invalid/p'))
                db.commit()
        self.source,self.target=self.engines

    def export(self):
        filename=self.path/'download.db';snapshot(self.source,filename);return filename

    def test_round_trip_restores_all_data_and_keeps_safety_copy(self):
        result=restore(self.target,self.export(),self.path/'backups')
        self.assertEqual(result['tables']['price_history'],1)
        self.assertTrue(result['relogin_required'])
        with Session(self.target) as db:
            self.assertEqual(db.query(KnownSku).one().sku,'W10531315')
            self.assertEqual(db.query(PriceHistory).one().price,42)
        backup=list((self.path/'backups').glob('*.db'))
        self.assertEqual(len(backup),1)
        with closing(sqlite3.connect(backup[0])) as db:self.assertEqual(db.execute('SELECT sku FROM known_skus').fetchone()[0],'W11199342')

    def test_invalid_file_preserves_current_database(self):
        bad=self.path/'invalid.db';bad.write_text('not a database')
        with self.assertRaises(HTTPException):restore(self.target,bad,self.path/'backups')
        with Session(self.target) as db:self.assertEqual(db.query(KnownSku).one().sku,'W11199342')

    def test_missing_schema_is_rejected(self):
        file=self.export()
        with closing(sqlite3.connect(file)) as db:db.execute('DROP TABLE offer_observations');db.commit()
        with self.assertRaises(HTTPException):restore(self.target,file,self.path/'backups')

    def test_legacy_backup_restores_acceleration_disabled(self):
        from models.product import CrawlerControl

        with Session(self.source) as db:
            db.add(CrawlerControl(id=1, default_mode='precise'))
            db.commit()
        file = self.export()
        with closing(sqlite3.connect(file)) as db:
            db.execute('ALTER TABLE crawler_control DROP COLUMN hardware_acceleration')
            db.commit()
        restore(self.target, file, self.path/'backups')
        with Session(self.target) as db:
            self.assertFalse(db.get(CrawlerControl, 1).hardware_acceleration)

    def test_running_extraction_blocks_import(self):
        with Session(self.target) as db:
            db.add(ExtractionJob(id='active',request_key='active',status='running'));db.commit()
        with self.assertRaises(HTTPException) as error:restore(self.target,self.export(),self.path/'backups')
        self.assertEqual(error.exception.status_code,409)

    def test_inconsistent_foreign_keys_roll_back_entire_restore(self):
        file=self.export()
        with closing(sqlite3.connect(file)) as db:db.execute('UPDATE price_history SET product_id=987654');db.commit()
        with self.assertRaises(HTTPException):restore(self.target,file,self.path/'backups')
        with Session(self.target) as db:self.assertEqual(db.query(KnownSku).one().sku,'W11199342')

    def test_running_checkpoints_recover_without_migrating_bearer_tokens(self):
        from datetime import datetime,timedelta
        from models.product import ExtractionJobItem
        with Session(self.source) as db:
            db.add(AuthSession(user_id=1,token_hash='old-session',expires_at=datetime.utcnow()+timedelta(days=1)))
            job=ExtractionJob(id='resume',request_key='resume',status='running')
            job.items=[ExtractionJobItem(sku='W10531315',position=0,status='running',attempts=4)]
            db.add(job);db.commit()
        restore(self.target,self.export(),self.path/'backups')
        with Session(self.target) as db:
            job=db.get(ExtractionJob,'resume')
            self.assertEqual(job.status,'queued');self.assertEqual(job.items[0].attempts,4)
            self.assertEqual(job.items[0].status,'queued');self.assertEqual(db.query(AuthSession).count(),0)
