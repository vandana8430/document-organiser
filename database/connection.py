"""SQLite connection and backward-compatible schema initialization."""
from pathlib import Path
import os
import sqlite3
import threading
from contextlib import contextmanager
PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _project_path(value, default):
    """Resolve configured relative paths against the project root, never CWD."""
    path = Path(value) if value else default
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()

DB_PATH = _project_path(os.getenv("SMARTFILES_DB_PATH"), PROJECT_ROOT / "data.db")
UPLOAD_DIR = _project_path(os.getenv("SMARTFILES_UPLOAD_DIR"), PROJECT_ROOT / "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(str(DB_PATH), check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT, salt TEXT);
CREATE TABLE IF NOT EXISTS sessions(tok TEXT PRIMARY KEY, uid INT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, uid INT, name TEXT, path TEXT, kind TEXT, sha TEXT,
  category TEXT, tags TEXT, summary TEXT, text TEXT, emb BLOB, created REAL, opened INT DEFAULT 0, last REAL, clip BLOB, embedder TEXT,
  extraction_status TEXT NOT NULL DEFAULT 'unknown', extraction_error TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, fid INT, uid INT, text TEXT, start REAL, emb BLOB, embedder TEXT);
CREATE TABLE IF NOT EXISTS compression_history(
  id INTEGER PRIMARY KEY, uid INT NOT NULL, original_fid INT, original_name TEXT NOT NULL,
  original_path TEXT, compressed_path TEXT NOT NULL, file_type TEXT NOT NULL,
  original_size INTEGER NOT NULL, compressed_size INTEGER NOT NULL,
  space_saved INTEGER NOT NULL, percent_reduction REAL NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plagiarism_scans(
  id INTEGER PRIMARY KEY, uid INT NOT NULL, fid INT NOT NULL, document_name TEXT NOT NULL,
  online_requested INT NOT NULL DEFAULT 0, status TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, report_json TEXT NOT NULL);
""")
DB.execute("CREATE INDEX IF NOT EXISTS plagiarism_scan_owner_date ON plagiarism_scans(uid,created DESC)")
DB.execute("CREATE INDEX IF NOT EXISTS plagiarism_scan_owner_file_date ON plagiarism_scans(uid,fid,created DESC)")
def _ensure_column(table, column, declaration):
    columns = {row["name"] for row in DB.execute("PRAGMA table_info(" + table + ")")}
    if column not in columns:
        DB.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)
_ensure_column("files", "clip", "BLOB")
_ensure_column("files", "embedder", "TEXT")
_ensure_column("files", "demo_key", "TEXT")
_ensure_column("chunks", "embedder", "TEXT")
_ensure_column("files", "extraction_status", "TEXT NOT NULL DEFAULT 'unknown'")
_ensure_column("files", "extraction_error", "TEXT")
DB.execute(
    "UPDATE files SET extraction_status=CASE "
    "WHEN trim(coalesce(text,''))='' THEN 'needs_attention' "
    "WHEN EXISTS (SELECT 1 FROM chunks WHERE chunks.fid=files.id AND trim(coalesce(chunks.text,''))!='') THEN 'indexed' "
    "ELSE 'extracted' END, "
    "extraction_error=CASE WHEN trim(coalesce(text,''))='' "
    "THEN 'No extracted text is saved. Use Repair text index to retry this original file.' ELSE NULL END "
    "WHERE extraction_status IS NULL OR extraction_status='unknown'"
)
DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS files_user_demo_key ON files(uid,demo_key) WHERE demo_key IS NOT NULL")
DB.commit()
_DB_LOCK = threading.RLock()

def stored_file_path(value):
    """Resolve both legacy relative upload paths and newer absolute paths."""
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()

@contextmanager
def transaction():
    """Run a group of SQLite writes atomically on the shared connection."""
    with _DB_LOCK:
        try:
            yield DB
            DB.commit()
        except Exception:
            DB.rollback()
            raise
