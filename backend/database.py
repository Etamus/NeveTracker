from pathlib import Path
import os
import re

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BACKEND_DIR / "precos.db"

load_dotenv(BACKEND_DIR / ".env")
load_dotenv(BACKEND_DIR.parent / ".env")


def _sqlite_url(path: Path) -> str:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{resolved.as_posix()}"


def _normalize_database_url(database_url):
    if not database_url:
        print(f"ATENCAO: Usando SQLite local em {DEFAULT_DB_PATH}")
        return _sqlite_url(DEFAULT_DB_PATH)

    if database_url.startswith("sqlite:///"):
        raw_path = database_url[len("sqlite:///"):].strip().strip('"').replace("\\", "/")
        if raw_path == ":memory:":
            return database_url

        if raw_path.startswith("./"):
            raw_path = raw_path[2:]

        is_windows_absolute = re.match(r"^[A-Za-z]:/", raw_path) is not None
        is_posix_absolute = raw_path.startswith("/")
        if raw_path:
            db_path = (
                Path(raw_path)
                if is_windows_absolute or is_posix_absolute
                else BACKEND_DIR / raw_path
            )
            try:
                return _sqlite_url(db_path)
            except OSError:
                print(
                    "ATENCAO: DATABASE_URL invalido. "
                    f"Usando SQLite local em {DEFAULT_DB_PATH}"
                )
                return _sqlite_url(DEFAULT_DB_PATH)

    return database_url


DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL"))
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
