import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path


# Shared language enum used across request schemas, LLM prompts, and DB validation.
class LANGS(str, Enum):
    en = "en"
    hi = "hi"
    hinglish = "hinglish"


# Return the configured SQLite database file path so tests or env overrides work cleanly.
def get_db_path() -> str:
    return os.getenv("DB_PATH", "zenvo.db")


# Return the current UTC timestamp in ISO-8601 format for consistent DB auditing.
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Open a SQLite connection with Row factory so columns can be accessed by name.
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# Rename any legacy DB whose sessions table lacks started_at so a fresh schema can be created.
def backup_legacy_db_if_needed() -> None:
    db_path = get_db_path()
    if db_path == ":memory:" or not Path(db_path).exists():
        return

    conn = get_connection()
    needs_backup = False
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if table is not None:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "started_at" not in columns:
                needs_backup = True
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except sqlite3.Error:
                    pass
    finally:
        conn.close()

    if needs_backup:
        bak_path = Path(db_path).with_name("zenvo.db.bak")
        os.replace(db_path, bak_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        print(f"[db] Old sessions schema detected; renamed {db_path} to {bak_path} and creating fresh DB.")


# Create all required tables and indexes on startup after checking for legacy schema migration.
def init_db() -> None:
    backup_legacy_db_if_needed()
    lang_values = ", ".join(f"'{lang.value}'" for lang in LANGS)
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                name TEXT,
                email TEXT,
                budget TEXT,
                need TEXT,
                timeline TEXT,
                score INTEGER DEFAULT 0,
                sentiment TEXT DEFAULT 'neutral',
                status TEXT DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                language TEXT DEFAULT 'en' CHECK(language IN ({lang_values})),
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'ended')),
                pending_slot TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                lead_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                language TEXT DEFAULT 'en' CHECK(language IN ({lang_values})),
                sentiment TEXT DEFAULT 'neutral',
                score INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                slot_datetime TEXT NOT NULL,
                status TEXT DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_slot
            ON appointments(slot_datetime)
            WHERE status = 'confirmed'
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                lead_id INTEGER NOT NULL,
                summary TEXT,
                objections TEXT,
                buying_signals TEXT,
                next_action TEXT,
                overall_sentiment TEXT,
                final_score INTEGER DEFAULT 0,
                sentiment_timeline TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# Map a numeric lead score to a funnel status while preserving an already booked status.
def compute_lead_status(score: int, current_status: str | None = None) -> str:
    if current_status == "booked":
        return "booked"
    if score >= 70:
        return "hot"
    if score >= 40:
        return "warm"
    return "new"


# Look up a lead by phone number or create a fresh lead so returning callers keep context.
def find_or_create_lead(phone: str | None) -> tuple[dict, bool]:
    clean_phone = phone.strip() if phone else None
    if clean_phone == "":
        clean_phone = None

    conn = get_connection()
    try:
        if clean_phone is not None:
            row = conn.execute(
                "SELECT * FROM leads WHERE phone = ?", (clean_phone,)
            ).fetchone()
            if row is not None:
                return dict(row), True

        timestamp = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO leads (phone, score, sentiment, status, created_at, updated_at)
            VALUES (?, 0, 'neutral', 'new', ?, ?)
            """,
            (clean_phone, timestamp, timestamp),
        )
        conn.commit()
        new_row = conn.execute(
            "SELECT * FROM leads WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(new_row), False
    finally:
        conn.close()


# Fetch a single lead dictionary by ID so handlers and prompts can read saved details.
def get_lead(lead_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# Save a new active call session linked to a lead along with its starting language.
def create_session(session_id: str, lead_id: int, language: str) -> None:
    lang_value = LANGS(language).value
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sessions (session_id, lead_id, language, status, pending_slot, started_at, ended_at)
            VALUES (?, ?, ?, 'active', NULL, ?, NULL)
            """,
            (session_id, lead_id, lang_value, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


# Retrieve session metadata by session_id to check existence, status, language, and pending_slot.
def get_session(session_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# Update the session's pending_slot column inside the caller's active transaction connection.
def update_pending_slot(
    session_id: str, pending_slot: str | None, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "UPDATE sessions SET pending_slot = ? WHERE session_id = ?",
        (pending_slot, session_id),
    )


# Count how many user turns have already been saved for a session to enforce turn caps.
def count_user_turns(session_id: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0
    finally:
        conn.close()


# Load the last N messages for a session in chronological order for LLM turn context.
def get_recent_messages(session_id: str, limit: int = 10) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) sub
            ORDER BY id ASC
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# Load every message for a session in chronological order for end-of-call analysis.
def get_all_messages(session_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# Persist a single user or agent message row into the messages table.
def save_message(
    session_id: str,
    lead_id: int,
    role: str,
    text: str,
    language: str,
    sentiment: str = "neutral",
    score: int = 0,
    conn: sqlite3.Connection | None = None,
) -> None:
    lang_value = LANGS(language).value
    own_conn = conn is None
    active_conn = get_connection() if own_conn else conn
    try:
        active_conn.execute(
            """
            INSERT INTO messages (session_id, lead_id, role, text, language, sentiment, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, lead_id, role, text, lang_value, sentiment, score, now_iso()),
        )
        if own_conn:
            active_conn.commit()
    finally:
        if own_conn:
            active_conn.close()


# Merge non-empty extracted fields and optional scoring into an existing lead record.
def merge_lead_fields(
    lead_id: int,
    extracted: dict | None = None,
    score: int | None = None,
    sentiment: str | None = None,
    force_status: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    own_conn = conn is None
    active_conn = get_connection() if own_conn else conn
    try:
        row = active_conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return
        lead = dict(row)
        extracted = extracted or {}

        allowed_fields = ("name", "email", "budget", "need", "timeline")
        updates = {}
        for field in allowed_fields:
            value = extracted.get(field)
            if value is not None and str(value).strip() != "":
                updates[field] = str(value).strip()

        new_score = lead["score"] if score is None else max(0, min(100, int(score)))
        new_sentiment = sentiment if sentiment in ("positive", "neutral", "negative") else lead["sentiment"]
        if force_status:
            new_status = force_status
        else:
            new_status = compute_lead_status(new_score, lead["status"])

        active_conn.execute(
            """
            UPDATE leads
            SET name = ?,
                email = ?,
                budget = ?,
                need = ?,
                timeline = ?,
                score = ?,
                sentiment = ?,
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                updates.get("name", lead["name"]),
                updates.get("email", lead["email"]),
                updates.get("budget", lead["budget"]),
                updates.get("need", lead["need"]),
                updates.get("timeline", lead["timeline"]),
                new_score,
                new_sentiment,
                new_status,
                now_iso(),
                lead_id,
            ),
        )
        if own_conn:
            active_conn.commit()
    finally:
        if own_conn:
            active_conn.close()


# Execute turn message and lead persistence on the caller's outer transaction connection without committing.
def save_turn(
    session_id: str,
    lead_id: int,
    user_text: str,
    user_language: str,
    result: dict,
    conn: sqlite3.Connection,
) -> None:
    reply_language = result.get("language", user_language)
    sentiment = result.get("sentiment", "neutral")
    lead_score = int(result.get("lead_score", 0))
    is_fallback = bool(result.get("fallback", False))
    force_status = "booked" if result.get("booked") else None

    save_message(
        session_id=session_id,
        lead_id=lead_id,
        role="user",
        text=user_text,
        language=user_language,
        sentiment=sentiment,
        score=lead_score,
        conn=conn,
    )
    save_message(
        session_id=session_id,
        lead_id=lead_id,
        role="agent",
        text=result.get("reply", ""),
        language=reply_language,
        sentiment=sentiment,
        score=lead_score,
        conn=conn,
    )
    if not is_fallback:
        merge_lead_fields(
            lead_id=lead_id,
            extracted=result.get("extracted"),
            score=lead_score,
            sentiment=sentiment,
            force_status=force_status,
            conn=conn,
        )


# Generate all 30-minute candidate slots between 10:00 and 17:00 for the next N days.
def generate_candidate_slots(days: int = 3) -> list[str]:
    today = datetime.now().date()
    slots: list[str] = []
    for day_offset in range(1, days + 1):
        slot_date = today + timedelta(days=day_offset)
        current = datetime.combine(slot_date, datetime.min.time()).replace(hour=10, minute=0)
        end_time = datetime.combine(slot_date, datetime.min.time()).replace(hour=17, minute=0)
        while current < end_time:
            slots.append(current.strftime("%Y-%m-%d %H:%M"))
            current += timedelta(minutes=30)
    return slots


# Return upcoming free slots by checking confirmed appointments within the 3-day candidate window.
def get_available_slots(days: int = 3, conn: sqlite3.Connection | None = None) -> list[str]:
    candidates = generate_candidate_slots(days=days)
    if not candidates:
        return []

    own_conn = conn is None
    active_conn = get_connection() if own_conn else conn
    try:
        rows = active_conn.execute(
            """
            SELECT slot_datetime FROM appointments
            WHERE status = 'confirmed'
              AND slot_datetime >= ?
              AND slot_datetime <= ?
            """,
            (candidates[0], candidates[-1]),
        ).fetchall()
        taken = {row["slot_datetime"] for row in rows}
        return [slot for slot in candidates if slot not in taken]
    finally:
        if own_conn:
            active_conn.close()


# Insert a confirmed appointment inside a SAVEPOINT on the outer transaction without committing.
def book_slot(
    lead_id: int,
    slot_datetime: str | None,
    conn: sqlite3.Connection,
) -> bool:
    if not slot_datetime:
        return False

    timestamp = now_iso()
    conn.execute("SAVEPOINT book_slot")
    try:
        conn.execute(
            """
            INSERT INTO appointments (lead_id, slot_datetime, status, created_at)
            VALUES (?, ?, 'confirmed', ?)
            """,
            (lead_id, slot_datetime, timestamp),
        )
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO book_slot")
        conn.execute("RELEASE book_slot")
        return False

    conn.execute("RELEASE book_slot")
    return True


# Fetch an existing saved call summary for idempotent /end_call responses.
def get_call_summary(session_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    own_conn = conn is None
    active_conn = get_connection() if own_conn else conn
    try:
        row = active_conn.execute(
            "SELECT * FROM calls WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "summary": row["summary"] or "",
            "objections": json.loads(row["objections"] or "[]"),
            "buying_signals": json.loads(row["buying_signals"] or "[]"),
            "next_best_action": row["next_action"] or "",
            "overall_sentiment": row["overall_sentiment"] or "neutral",
            "final_score": int(row["final_score"] or 0),
            "sentiment_timeline": json.loads(row["sentiment_timeline"] or "[]"),
        }
    finally:
        if own_conn:
            active_conn.close()


# Save the call summary, update the lead only if not fallback, and handle overlapping /end_call safely.
def finalize_call(
    session_id: str,
    lead_id: int,
    summary_data: dict,
    is_fallback: bool = False,
) -> dict:
    conn = get_connection()
    timestamp = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO calls (
                    session_id, lead_id, summary, objections, buying_signals,
                    next_action, overall_sentiment, final_score, sentiment_timeline, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    lead_id,
                    str(summary_data.get("summary", "")),
                    json.dumps(summary_data.get("objections", [])),
                    json.dumps(summary_data.get("buying_signals", [])),
                    str(summary_data.get("next_best_action", "")),
                    str(summary_data.get("overall_sentiment", "neutral")),
                    int(summary_data.get("final_score", 0)),
                    json.dumps(summary_data.get("sentiment_timeline", [])),
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = get_call_summary(session_id, conn=conn)
            if existing is not None:
                return existing
            raise

        if not is_fallback:
            merge_lead_fields(
                lead_id=lead_id,
                score=summary_data["final_score"],
                sentiment=summary_data["overall_sentiment"],
                conn=conn,
            )
        conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = ? WHERE session_id = ?",
            (timestamp, session_id),
        )
        conn.commit()
        return summary_data
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
