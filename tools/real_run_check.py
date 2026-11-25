import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REQUEST_GAP_SECONDS = 6.0
RATE_LIMIT_COOLDOWN_SECONDS = 12.0
FALLBACK_MARKERS = (
    "brief connection issue",
    "network samasya",
    "network issue aa gaya",
)
BOOKED_TEMPLATE_PREFIX = "Done! Your demo is booked for "


# Mask phone numbers and emails so no PII is ever printed in logs or failure details.
def mask_phone(phone: str | None) -> str:
    if not phone:
        return "none"
    s = str(phone)
    if len(s) <= 4:
        return "***"
    return f"{s[:3]}***{s[-4:]}"


# Remove email or phone fields from any DB row dictionary before printing diagnostics.
def sanitize_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    cleaned = dict(row)
    if "phone" in cleaned:
        cleaned["phone"] = mask_phone(cleaned["phone"])
    if "email" in cleaned and cleaned["email"]:
        cleaned["email"] = "***@***"
    return cleaned


# Resolve the SQLite DB path from .env or default and open it strictly read-only.
def open_readonly_db() -> sqlite3.Connection:
    load_dotenv()
    db_path = Path(os.getenv("DB_PATH", "zenvo.db")).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite DB not found at {db_path}")
    uri = f"{db_path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# Detect whether a /turn reply came from brain.fallback_reply without trusting other fields.
def is_fallback_reply(resp_json: dict) -> bool:
    reply = str(resp_json.get("reply", "")).lower()
    return any(marker in reply for marker in FALLBACK_MARKERS)


# Format a 'YYYY-MM-DD HH:MM' slot string into a natural weekday + date + 12-hour label.
def format_natural_slot(slot_dt: str) -> str:
    dt = datetime.strptime(slot_dt, "%Y-%m-%d %H:%M")
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%A')}, {dt.strftime('%Y-%m-%d')} at {hour}:{dt.strftime('%M %p')}"


# Pick a free weekday 11:00 AM slot from /api/slots (preferring 3 days out if weekday, else latest weekday).
def pick_target_slot(slots: list[str]) -> str:
    today = datetime.now().date()
    weekday_11am = [
        s for s in slots
        if s.endswith(" 11:00") and datetime.strptime(s, "%Y-%m-%d %H:%M").date().weekday() < 5
    ]
    three_days = [
        s for s in weekday_11am
        if (datetime.strptime(s, "%Y-%m-%d %H:%M").date() - today).days == 3
    ]
    if three_days:
        return three_days[0]
    if weekday_11am:
        return weekday_11am[-1]
    any_11am = [s for s in slots if s.endswith(" 11:00")]
    if any_11am:
        return any_11am[0]
    return slots[0]


# Call POST /start_call with a unique test phone suffix so each check starts cleanly.
def start_session(client: httpx.Client, base_url: str, language: str, tag: int) -> dict:
    unique_phone = f"+9199{int(time.time()) % 1000000:06d}{tag:02d}"
    resp = client.post(
        f"{base_url}/start_call",
        json={"phone": unique_phone, "language": language},
    )
    resp.raise_for_status()
    return resp.json()


# Pause politely before LLM calls so upstream rate limits are not triggered.
def wait_before_llm_call(turn_metrics: list[dict]) -> None:
    if not turn_metrics:
        return
    gap = RATE_LIMIT_COOLDOWN_SECONDS if turn_metrics[-1]["fallback"] else REQUEST_GAP_SECONDS
    time.sleep(gap)


# Call POST /turn, measure round-trip latency, and record fallback status.
def send_turn(
    client: httpx.Client,
    base_url: str,
    session_id: str,
    text: str,
    language: str | None,
    turn_metrics: list[dict],
    label: str,
) -> tuple[int, dict, dict]:
    wait_before_llm_call(turn_metrics)
    payload: dict = {"session_id": session_id, "text": text}
    if language is not None:
        payload["language"] = language

    start = time.perf_counter()
    resp = client.post(f"{base_url}/turn", json=payload)
    latency = time.perf_counter() - start
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
    fb = is_fallback_reply(data) if resp.status_code == 200 else False

    turn_metrics.append({"label": label, "latency": latency, "fallback": fb, "status": resp.status_code})
    return resp.status_code, data, payload


# Query a single session row from the read-only DB.
def fetch_session_row(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


# Query active confirmed appointments for a specific slot_datetime.
def fetch_slot_appointments(conn: sqlite3.Connection, slot_datetime: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM appointments WHERE slot_datetime = ? AND status = 'confirmed'",
        (slot_datetime,),
    ).fetchall()
    return [dict(r) for r in rows]


# Query total active confirmed appointment count in the DB.
def count_confirmed_appointments(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'confirmed'").fetchone()[0]
    )


# Run check 1 (English pricing enquiry) with one retry on assertion failure.
def check_pricing(client: httpx.Client, base_url: str, metrics: list[dict]) -> dict:
    req_text = "What's the price of the Pro plan?"
    retried = False
    last_req, last_resp = {}, {}

    for attempt in range(2):
        if attempt == 1:
            retried = True
        sess = start_session(client, base_url, "en", tag=10 + attempt)
        status, data, req = send_turn(client, base_url, sess["session_id"], req_text, "en", metrics, f"pricing#{attempt+1}")
        last_req, last_resp = req, data
        reply = str(data.get("reply", ""))
        fb = is_fallback_reply(data)
        ok = status == 200 and ("2499" in reply or "2,499" in reply) and not fb and data.get("language") == "en"
        if ok:
            note = " (retried once)" if retried else ""
            return {"check": "1. PRICING", "status": "PASS", "reason": f"Pro price mentioned in English{note}"}

    return {
        "check": "1. PRICING",
        "status": "FAIL",
        "reason": "Reply missing 2499/2,499, wrong language, or fallback used",
        "request": last_req,
        "reply": last_resp.get("reply"),
        "db_rows": [],
        "guess": "LLM omitted the Pro price figure or returned fallback due to rate-limit/timeout.",
    }


# Run check 2 (Hindi/Hinglish Basic plan enquiry) with one retry on assertion failure.
def check_hindi(client: httpx.Client, base_url: str, metrics: list[dict]) -> dict:
    req_text = "Mujhe Basic plan ke baare mein batao"
    retried = False
    last_req, last_resp = {}, {}

    for attempt in range(2):
        if attempt == 1:
            retried = True
        sess = start_session(client, base_url, "hi", tag=20 + attempt)
        status, data, req = send_turn(client, base_url, sess["session_id"], req_text, None, metrics, f"hindi#{attempt+1}")
        last_req, last_resp = req, data
        reply = str(data.get("reply", ""))
        fb = is_fallback_reply(data)
        ok = status == 200 and ("999" in reply) and data.get("language") in ("hi", "hinglish") and not fb
        if ok:
            note = " (retried once)" if retried else ""
            return {"check": "2. HINDI", "status": "PASS", "reason": f"Basic Rs 999 answered in {data.get('language')}{note}"}

    return {
        "check": "2. HINDI",
        "status": "FAIL",
        "reason": "Reply missing 999, wrong language, or fallback used",
        "request": last_req,
        "reply": last_resp.get("reply"),
        "db_rows": [],
        "guess": "LLM omitted 999, classified Hinglish as 'en', or hit a provider error/fallback.",
    }


# Run check 3 (two-step booking proposal) and return session/slot state for checks 4 and 6.
def check_booking_propose(
    client: httpx.Client, base_url: str, conn: sqlite3.Connection, metrics: list[dict]
) -> tuple[dict, str, str, int]:
    slots = client.get(f"{base_url}/api/slots").json().get("slots", [])
    if not slots:
        return (
            {
                "check": "3. BOOKING PROPOSE",
                "status": "FAIL",
                "reason": "No free slots returned by /api/slots",
                "request": {"GET": "/api/slots"},
                "reply": None,
                "db_rows": [],
                "guess": "All candidate slots for the next 3 days are already booked.",
            },
            "",
            "",
            0,
        )

    target_slot = pick_target_slot(slots)
    natural_slot = format_natural_slot(target_slot)
    before_appts = count_confirmed_appointments(conn)

    sess = start_session(client, base_url, "en", tag=30)
    session_id = sess["session_id"]

    prompts = [
        "My name is Test User and I run a clinic.",
        f"Could we schedule a demo on {natural_slot}?",
        f"Please check and propose the slot {target_slot} for my demo.",
        f"I only want {target_slot}. Is {target_slot} available?",
    ]

    last_req, last_resp = {}, {}
    pending_slot = None
    turns_used = 0

    for idx, text in enumerate(prompts):
        status, data, req = send_turn(client, base_url, session_id, text, "en", metrics, f"propose#{idx+1}")
        last_req, last_resp = req, data
        turns_used += 1
        row = fetch_session_row(conn, session_id)
        pending_slot = row.get("pending_slot") if row else None
        after_appts = count_confirmed_appointments(conn)
        if idx >= 1 and pending_slot and after_appts == before_appts:
            extra_note = f" (after {idx - 1} extra turn)" if idx > 1 else ""
            return (
                {
                    "check": "3. BOOKING PROPOSE",
                    "status": "PASS",
                    "reason": f"pending_slot set to {pending_slot} with 0 new appointments{extra_note}",
                },
                session_id,
                pending_slot,
                before_appts,
            )

    sess_row = fetch_session_row(conn, session_id)
    after_appts = count_confirmed_appointments(conn)
    return (
        {
            "check": "3. BOOKING PROPOSE",
            "status": "FAIL",
            "reason": f"pending_slot={pending_slot!r}, new_appts={after_appts - before_appts} after {turns_used} turns",
            "request": last_req,
            "reply": last_resp.get("reply"),
            "db_rows": [sess_row] if sess_row else [],
            "guess": "LLM did not output an exact free slot in slot_datetime or already booked prematurely.",
        },
        session_id,
        target_slot,
        before_appts,
    )


# Run check 4 (confirm the proposed slot) with one retry if needed.
def check_booking_confirm(
    client: httpx.Client,
    base_url: str,
    conn: sqlite3.Connection,
    session_id: str,
    target_slot: str,
    before_appts: int,
    metrics: list[dict],
) -> dict:
    if not session_id or not target_slot:
        return {
            "check": "4. BOOKING CONFIRM",
            "status": "FAIL",
            "reason": "Skipped because Check 3 failed to set up a session/slot",
            "request": {},
            "reply": None,
            "db_rows": [],
            "guess": "Prerequisite booking proposal check did not produce a pending_slot.",
        }

    retried = False
    prompts = ["Yes, confirm that slot", f"Yes, confirm {target_slot}"]
    last_req, last_resp = {}, {}

    for attempt, text in enumerate(prompts):
        if attempt == 1:
            retried = True
        status, data, req = send_turn(client, base_url, session_id, text, "en", metrics, f"confirm#{attempt+1}")
        last_req, last_resp = req, data

        after_appts = count_confirmed_appointments(conn)
        sess_row = fetch_session_row(conn, session_id)
        slot_appts = fetch_slot_appointments(conn, target_slot)
        reply = str(data.get("reply", ""))

        ok = (
            status == 200
            and after_appts == before_appts + 1
            and len(slot_appts) == 1
            and sess_row is not None
            and sess_row.get("pending_slot") is None
            and reply.startswith(BOOKED_TEMPLATE_PREFIX)
            and reply.endswith("You will get a confirmation shortly.")
        )
        if ok:
            note = " (retried once)" if retried else ""
            return {
                "check": "4. BOOKING CONFIRM",
                "status": "PASS",
                "reason": f"Confirmed 1 appointment for {target_slot}, cleared pending_slot, returned template{note}",
            }

    sess_row = fetch_session_row(conn, session_id)
    slot_appts = fetch_slot_appointments(conn, target_slot)
    return {
        "check": "4. BOOKING CONFIRM",
        "status": "FAIL",
        "reason": "Did not create exact 1 appointment, clear pending_slot, or return template",
        "request": last_req,
        "reply": last_resp.get("reply"),
        "db_rows": ([sess_row] if sess_row else []) + slot_appts,
        "guess": "LLM did not return intent=book_appointment with the matching pending slot.",
    }


# Run check 5 (new session attempts to double-book the already confirmed slot) and check idx_active_slot.
def check_double_book(
    client: httpx.Client,
    base_url: str,
    conn: sqlite3.Connection,
    target_slot: str,
    metrics: list[dict],
) -> dict:
    idx_row = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND name='idx_active_slot'"
    ).fetchone()
    idx_exists = idx_row is not None

    if not target_slot:
        return {
            "check": "5. DOUBLE BOOK",
            "status": "FAIL",
            "reason": f"No target slot available; idx_active_slot={idx_exists}",
            "request": {},
            "reply": None,
            "db_rows": [],
            "guess": "Prerequisite booking step did not establish a booked slot.",
        }

    sess = start_session(client, base_url, "en", tag=50)
    session_id = sess["session_id"]
    natural_slot = format_natural_slot(target_slot)

    send_turn(
        client,
        base_url,
        session_id,
        f"I want a demo on {natural_slot} ({target_slot}).",
        "en",
        metrics,
        "double_book#1",
    )
    status, data, req2 = send_turn(
        client,
        base_url,
        session_id,
        f"Yes, please confirm {target_slot}.",
        "en",
        metrics,
        "double_book#2",
    )

    slot_appts = fetch_slot_appointments(conn, target_slot)
    reply = str(data.get("reply", ""))
    reply_lower = reply.lower()
    offers_alternative = any(
        kw in reply_lower for kw in ("taken", "another", "instead", "available", "other", "slot", "book")
    ) and not reply.startswith(BOOKED_TEMPLATE_PREFIX)

    if status == 200 and len(slot_appts) == 1 and offers_alternative and idx_exists:
        return {
            "check": "5. DOUBLE BOOK",
            "status": "PASS",
            "reason": f"Blocked 2nd booking (count=1), offered alternative, idx_active_slot present",
        }

    return {
        "check": "5. DOUBLE BOOK",
        "status": "FAIL",
        "reason": f"slot_appts={len(slot_appts)}, offers_alt={offers_alternative}, idx_active_slot={idx_exists}",
        "request": req2,
        "reply": reply,
        "db_rows": slot_appts + ([dict(idx_row)] if idx_row else []),
        "guess": "Either a duplicate appointment bypassed constraints, index is missing, or reply looked like confirmation.",
    }


# Run check 6 (/end_call called twice on the booking session).
def check_end_call_twice(
    client: httpx.Client,
    base_url: str,
    conn: sqlite3.Connection,
    session_id: str,
) -> dict:
    if not session_id:
        return {
            "check": "6. END_CALL TWICE",
            "status": "FAIL",
            "reason": "Skipped because Check 3-4 session was not created",
            "request": {},
            "reply": None,
            "db_rows": [],
            "guess": "Prerequisite booking session missing.",
        }

    time.sleep(REQUEST_GAP_SECONDS)
    req = {"session_id": session_id}
    r1 = client.post(f"{base_url}/end_call", json=req)
    r2 = client.post(f"{base_url}/end_call", json=req)
    d1 = r1.json() if r1.status_code == 200 else {}
    d2 = r2.json() if r2.status_code == 200 else {}

    call_rows = [
        dict(r)
        for r in conn.execute("SELECT * FROM calls WHERE session_id = ?", (session_id,)).fetchall()
    ]

    ok = (
        r1.status_code == 200
        and r2.status_code == 200
        and d1.get("summary") == d2.get("summary")
        and d1.get("final_score") == d2.get("final_score")
        and len(call_rows) == 1
    )
    if ok:
        return {
            "check": "6. END_CALL TWICE",
            "status": "PASS",
            "reason": f"Both returned 200 with identical summary/score ({d1.get('final_score')}) and 1 calls row",
        }

    return {
        "check": "6. END_CALL TWICE",
        "status": "FAIL",
        "reason": f"statuses=({r1.status_code},{r2.status_code}), calls_rows={len(call_rows)}",
        "request": req,
        "reply": json.dumps({"first_summary": d1.get("summary"), "second_summary": d2.get("summary")}),
        "db_rows": call_rows,
        "guess": "/end_call was not idempotent or failed on second invocation.",
    }


# Run check 7 (DB sanity counts and ended-session / unique calls verification).
def check_db_sanity(conn: sqlite3.Connection, booked_session_id: str) -> tuple[dict, dict]:
    appt_count = int(conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0])
    sess_counts = {
        row["status"]: int(row["cnt"])
        for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM sessions GROUP BY status").fetchall()
    }
    calls_count = int(conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
    dup_calls = [
        dict(r)
        for r in conn.execute(
            "SELECT session_id, COUNT(*) AS cnt FROM calls GROUP BY session_id HAVING COUNT(*) > 1"
        ).fetchall()
    ]
    booked_session = fetch_session_row(conn, booked_session_id) if booked_session_id else None

    ok = (
        booked_session is not None
        and booked_session.get("status") == "ended"
        and len(dup_calls) == 0
    )
    counts = {
        "appointments": appt_count,
        "sessions_by_status": sess_counts,
        "calls": calls_count,
    }

    if ok:
        return (
            {
                "check": "7. DB SANITY",
                "status": "PASS",
                "reason": f"booked session ended; appts={appt_count}, calls={calls_count}, duplicates=0",
            },
            counts,
        )

    return (
        {
            "check": "7. DB SANITY",
            "status": "FAIL",
            "reason": f"booked_status={booked_session.get('status') if booked_session else None}, dup_calls={len(dup_calls)}",
            "request": {"session_id": booked_session_id},
            "reply": None,
            "db_rows": ([booked_session] if booked_session else []) + dup_calls,
            "guess": "Booked session was not marked ended or calls contains duplicate session_id rows.",
        },
        counts,
    )


# Print formatted results table, failure details, latency/fallback summary, DB counts, and final verdict.
def print_report(results: list[dict], metrics: list[dict], counts: dict) -> None:
    print("\n=== REAL-RUN CHECK RESULTS ===")
    print(f"{'Check':<20} | {'Result':<6} | Reason")
    print("-" * 90)
    for r in results:
        print(f"{r['check']:<20} | {r['status']:<6} | {r['reason']}")

    failures = [r for r in results if r["status"] == "FAIL"]
    if failures:
        print("\n=== FAILURE DETAILS ===")
        for f in failures:
            print(f"\n[{f['check']}]")
            print(f"  Request : {json.dumps(f.get('request', {}))}")
            print(f"  Reply   : {f.get('reply')!r}")
            sanitized_rows = [sanitize_row(row) for row in f.get("db_rows", [])]
            print(f"  DB rows : {json.dumps(sanitized_rows, default=str)}")
            print(f"  Guess   : {f.get('guess')}")

    latencies = [m["latency"] for m in metrics]
    p50 = statistics.median(latencies) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0
    fallback_turns = [m["label"] for m in metrics if m["fallback"]]

    print("\n=== LATENCY & FALLBACK SUMMARY ===")
    for m in metrics:
        fb_flag = " [FALLBACK=true]" if m["fallback"] else ""
        print(f"  {m['label']:<16} status={m['status']} latency={m['latency']:.2f}s{fb_flag}")
    print(f"p50 latency: {p50:.2f}s | max latency: {max_lat:.2f}s")
    print(f"Fallback turns: {', '.join(fallback_turns) if fallback_turns else 'None'}")

    print("\n=== DB COUNTS ===")
    print(
        f"appointments={counts['appointments']} | "
        f"sessions_by_status={counts['sessions_by_status']} | "
        f"calls={counts['calls']}"
    )

    if not failures:
        print("\nVERDICT: ALL PASS")
    else:
        failed_names = ", ".join(f["check"] for f in failures)
        print(f"\nVERDICT: FAILED: {failed_names}")


# Run all 7 real-run checks against the already running server and read-only SQLite DB.
def main() -> None:
    base_url = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL).rstrip("/")
    conn = open_readonly_db()
    metrics: list[dict] = []
    results: list[dict] = []

    try:
        with httpx.Client(timeout=30.0) as client:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()

            results.append(check_pricing(client, base_url, metrics))
            results.append(check_hindi(client, base_url, metrics))

            propose_res, booked_session_id, target_slot, before_appts = check_booking_propose(
                client, base_url, conn, metrics
            )
            results.append(propose_res)

            results.append(
                check_booking_confirm(
                    client, base_url, conn, booked_session_id, target_slot, before_appts, metrics
                )
            )
            results.append(check_double_book(client, base_url, conn, target_slot, metrics))
            results.append(check_end_call_twice(client, base_url, conn, booked_session_id))

            sanity_res, counts = check_db_sanity(conn, booked_session_id)
            results.append(sanity_res)
    finally:
        conn.close()

    print_report(results, metrics, counts)


if __name__ == "__main__":
    main()
