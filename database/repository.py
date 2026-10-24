"""Small SQLite query repository backed by the shared project connection."""
from .connection import DB, _DB_LOCK

def q(sql, args=()):
    """Execute a query under the shared connection lock and return its rows."""
    with _DB_LOCK:
        cursor = DB.execute(sql, args)
        rows = cursor.fetchall() if cursor.description else []
        DB.commit()
        return rows

# Re-export common persistence helpers to keep router dependencies concise.
from .connection import UPLOAD_DIR, stored_file_path, transaction
