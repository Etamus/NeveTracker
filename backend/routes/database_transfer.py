from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from database import engine, BACKEND_DIR
from services.auth_service import require_developer
from services.database_transfer import restore, temporary_snapshot

database_transfer_router = APIRouter(dependencies=[Depends(require_developer)])


@database_transfer_router.get('/backup')
def download_database():
    filename = temporary_snapshot(engine)
    return FileResponse(filename, media_type='application/vnd.sqlite3',
                        filename='nevetracker-banco-completo.db',
                        background=BackgroundTask(Path(filename).unlink, missing_ok=True))


@database_transfer_router.post('/restore')
def import_database(file: UploadFile = File(...)):
    with tempfile.TemporaryDirectory(prefix='nevetracker-import-') as folder:
        filename = Path(folder) / 'import.db'
        size = 0
        with filename.open('wb') as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024**3:
                    raise HTTPException(413, 'O banco excede 2 GB.')
                output.write(chunk)
        return restore(engine, filename, BACKEND_DIR.parent / '.logs' / 'database-backups')
