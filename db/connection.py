"""
SQLite connection management. WAL mode for concurrent readers.
"""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection | None = None):
    """Context manager that commits on success, rolls back on exception."""
    if conn is None:
        conn = get_conn()
        close = True
    else:
        close = False
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        if close:
            conn.close()
