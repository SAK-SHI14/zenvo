import json
from datetime import datetime

from app import db

VALID_SENTIMENTS = {"positive", "neutral", "negative"}
VALID_STATUSES = {"new", "warm", "hot", "booked"}


# Mask phone numbers in Python so raw numbers are never sent to HTML templates.
def mask_phone(phone: str | None) -> str:
    if not phone or not str(phone).strip():
        return "Not provided"
    cleaned = str(phone).strip()
    if len(cleaned) <= 4:
        return "****"
    prefix = cleaned[:3] if cleaned.startswith("+") and len(cleaned) > 7 else ""
    masked_len = max(4, len(cleaned) - len(prefix) - 4)
    return f"{prefix}{'*' * masked_len}{cleaned[-4:]}"


# Format an ISO timestamp string into a concise readable UTC date and time.
def format_timestamp(raw_ts: str | None) -> str:
    if not raw_ts or not str(raw_ts).strip():
        return "—"
    text = str(raw_ts).strip()
    try:
        dt = datetime.fromisoformat(text)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]


# Parse a JSON-encoded list column safely, returning an empty list on NULL or invalid JSON.
def parse_json_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# Normalize a sentiment label to one of the supported badge classes.
def safe_sentiment(raw_sentiment: str | None) -> str:
    val = str(raw_sentiment or "neutral").lower().strip()
    return val if val in VALID_SENTIMENTS else "neutral"


# Normalize a lead qualification status label to one of the supported badge classes.
def safe_status(raw_status: str | None) -> str:
    val = str(raw_status or "new").lower().strip()
    return val if val in VALID_STATUSES else "new"


# Compute all dashboard KPIs using SQL aggregations with safe handling for zero records.
def fetch_kpis(conn) -> dict:
    lead_row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_leads,
            COALESCE(
                SUM(CASE WHEN COALESCE(score, 0) >= 40 OR status IN ('warm', 'hot', 'booked') THEN 1 ELSE 0 END),
                0
            ) AS qualified_leads,
            ROUND(COALESCE(AVG(score), 0), 1) AS avg_score
        FROM leads
        """
    ).fetchone()
    call_row = conn.execute("SELECT COUNT(*) AS completed_calls FROM calls").fetchone()
    appt_row = conn.execute(
        "SELECT COUNT(*) AS appointments_booked FROM appointments WHERE status = 'confirmed'"
    ).fetchone()

    total_leads = int(lead_row["total_leads"])
    qualified_leads = int(lead_row["qualified_leads"])
    avg_score = float(lead_row["avg_score"] or 0.0)
    completed_calls = int(call_row["completed_calls"])
    appointments_booked = int(appt_row["appointments_booked"])

    conversion_rate = round((appointments_booked / total_leads) * 100, 1) if total_leads > 0 else 0.0

    return {
        "total_leads": total_leads,
        "completed_calls": completed_calls,
        "qualified_leads": qualified_leads,
        "appointments_booked": appointments_booked,
        "avg_score": avg_score,
        "conversion_rate": conversion_rate,
        "conversion_note": f"{appointments_booked} booked / {total_leads} total leads",
    }


# Fetch all leads sorted by most recent interaction along with their latest session language.
def fetch_leads(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            l.id,
            l.phone,
            l.name,
            l.score,
            l.sentiment,
            l.status,
            l.need,
            l.updated_at,
            l.created_at,
            (
                SELECT s.language
                FROM sessions s
                WHERE s.lead_id = l.id
                ORDER BY s.started_at DESC
                LIMIT 1
            ) AS language
        FROM leads l
        ORDER BY COALESCE(l.updated_at, l.created_at) DESC, l.id DESC
        """
    ).fetchall()

    leads: list[dict] = []
    for row in rows:
        leads.append(
            {
                "id": row["id"],
                "name": row["name"] or "Unnamed Caller",
                "masked_phone": mask_phone(row["phone"]),
                "language": row["language"] or "—",
                "score": int(row["score"] or 0),
                "sentiment": safe_sentiment(row["sentiment"]),
                "status": safe_status(row["status"]),
                "need": row["need"] or "—",
                "last_interaction": format_timestamp(row["updated_at"] or row["created_at"]),
            }
        )
    return leads


# Fetch all appointments joined with lead name and masked phone for the dashboard table.
def fetch_appointments(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            a.id,
            a.slot_datetime,
            a.status,
            l.name,
            l.phone
        FROM appointments a
        LEFT JOIN leads l ON l.id = a.lead_id
        ORDER BY a.slot_datetime ASC, a.id DESC
        """
    ).fetchall()

    appointments: list[dict] = []
    for row in rows:
        display_name = row["name"] or mask_phone(row["phone"])
        appointments.append(
            {
                "id": row["id"],
                "lead_name": display_name,
                "slot_datetime": row["slot_datetime"] or "—",
                "status": row["status"] or "confirmed",
            }
        )
    return appointments


# Fetch completed calls sorted newest-first with masked caller identity and summary scores.
def fetch_recent_calls(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.final_score,
            c.overall_sentiment,
            c.created_at,
            l.name,
            l.phone
        FROM calls c
        LEFT JOIN leads l ON l.id = c.lead_id
        ORDER BY c.created_at DESC, c.id DESC
        """
    ).fetchall()

    calls: list[dict] = []
    for row in rows:
        caller_label = row["name"] or mask_phone(row["phone"])
        calls.append(
            {
                "id": row["id"],
                "caller_label": caller_label,
                "call_datetime": format_timestamp(row["created_at"]),
                "final_score": int(row["final_score"] or 0),
                "overall_sentiment": safe_sentiment(row["overall_sentiment"]),
            }
        )
    return calls


# Assemble the complete context dictionary needed to render the /dashboard page.
def get_dashboard_data() -> dict:
    conn = db.get_connection()
    try:
        return {
            "kpis": fetch_kpis(conn),
            "leads": fetch_leads(conn),
            "appointments": fetch_appointments(conn),
            "calls": fetch_recent_calls(conn),
        }
    finally:
        conn.close()


# Fetch a single call record, masked lead profile, insights, and transcript for /dashboard/calls/{call_id}.
def get_call_detail(call_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                c.id,
                c.session_id,
                c.lead_id,
                c.summary,
                c.objections,
                c.buying_signals,
                c.next_action,
                c.overall_sentiment,
                c.final_score,
                c.sentiment_timeline,
                c.created_at,
                l.name AS lead_name,
                l.phone AS lead_phone,
                l.budget AS lead_budget,
                l.need AS lead_need,
                l.timeline AS lead_timeline,
                l.status AS lead_status,
                s.language AS session_language
            FROM calls c
            LEFT JOIN leads l ON l.id = c.lead_id
            LEFT JOIN sessions s ON s.session_id = c.session_id
            WHERE c.id = ?
            """,
            (call_id,),
        ).fetchone()
        if row is None:
            return None

        msg_rows = conn.execute(
            """
            SELECT id, role, text, language, sentiment, score, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (row["session_id"],),
        ).fetchall()

        messages: list[dict] = []
        for msg in msg_rows:
            messages.append(
                {
                    "role": "agent" if msg["role"] == "agent" else "user",
                    "speaker_label": "Zenvo" if msg["role"] == "agent" else "Customer",
                    "text": msg["text"] or "",
                    "sentiment": safe_sentiment(msg["sentiment"]),
                    "score": int(msg["score"] or 0),
                    "timestamp": format_timestamp(msg["created_at"]),
                }
            )

        raw_timeline = parse_json_list(row["sentiment_timeline"])
        timeline = [safe_sentiment(item) for item in raw_timeline]

        return {
            "id": row["id"],
            "call_datetime": format_timestamp(row["created_at"]),
            "summary": row["summary"] or "No summary recorded.",
            "objections": parse_json_list(row["objections"]),
            "buying_signals": parse_json_list(row["buying_signals"]),
            "next_action": row["next_action"] or "No follow-up action recorded.",
            "overall_sentiment": safe_sentiment(row["overall_sentiment"]),
            "final_score": int(row["final_score"] or 0),
            "sentiment_timeline": timeline,
            "customer": {
                "name": row["lead_name"] or "Unnamed Caller",
                "masked_phone": mask_phone(row["lead_phone"]),
                "language": row["session_language"] or "—",
                "status": safe_status(row["lead_status"]),
                "need": row["lead_need"] or "—",
                "budget": row["lead_budget"] or "—",
                "timeline": row["lead_timeline"] or "—",
            },
            "messages": messages,
        }
    finally:
        conn.close()
