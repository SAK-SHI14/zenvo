import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.seed import seed_database

DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
TABLES = ("leads", "sessions", "messages", "appointments", "calls")


# Check whether the FastAPI server is currently reachable so we refuse to reset a live DB.
def is_server_running(health_url: str = DEFAULT_HEALTH_URL) -> bool:
    try:
        httpx.get(health_url, timeout=1.0)
        return True
    except httpx.HTTPError:
        return False


# Read row counts for every table so before/after totals can be printed cleanly.
def get_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0])
    return counts


# Back up the DB to zenvo.db.bak.<timestamp>, clear runtime rows, and restore seed demo data.
def reset_demo_db(db_path: Path | None = None, health_url: str = DEFAULT_HEALTH_URL) -> None:
    if is_server_running(health_url):
        print("stop the server first")
        sys.exit(1)

    load_dotenv()
    target_db = (db_path or Path(os.getenv("DB_PATH", "zenvo.db"))).resolve()
    if not target_db.exists():
        print(f"Database file not found: {target_db}")
        sys.exit(1)

    conn = sqlite3.connect(target_db, timeout=10.0)
    try:
        before_counts = get_row_counts(conn)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = target_db.with_name(f"{target_db.name}.bak.{timestamp}")
    shutil.copy2(target_db, backup_path)
    print(f"Backed up DB to {backup_path}")
    print(f"Row counts BEFORE: {before_counts}")

    conn = sqlite3.connect(target_db, timeout=10.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("appointments", "calls", "messages", "sessions", "leads"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    seed_database()

    conn = sqlite3.connect(target_db, timeout=10.0)
    try:
        after_counts = get_row_counts(conn)
    finally:
        conn.close()

    print(f"Row counts AFTER : {after_counts}")


if __name__ == "__main__":
    reset_demo_db()
