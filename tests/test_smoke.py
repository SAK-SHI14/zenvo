import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AuthenticationError, InternalServerError, RateLimitError

from app import brain, db, handlers, summary
from app.main import app


# Configure an isolated temporary SQLite database for every smoke test.
@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_zenvo.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    with TestClient(app) as test_client:
        yield test_client


# Verify two sessions proposing and confirming the same slot only allow one booking.
def test_double_booking(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    slot_human = handlers.format_slot_human(target_slot)

    s1 = client.post("/start_call", json={"phone": "+919800000001", "language": "en"}).json()["session_id"]
    s2 = client.post("/start_call", json={"phone": "+919800000002", "language": "en"}).json()["session_id"]

    propose_slot = {
        "reply": f"How about {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    confirm_slot = {
        "reply": "Model text that should be replaced",
        "language": "en",
        "intent": "book_appointment",
        "sentiment": "positive",
        "lead_score": 90,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }

    with patch("app.brain.generate_turn_response", return_value=propose_slot):
        assert client.post("/turn", json={"session_id": s1, "text": "When can we meet?"}).status_code == 200
        assert client.post("/turn", json={"session_id": s2, "text": "When can we meet?"}).status_code == 200

    with patch("app.brain.generate_turn_response", return_value=confirm_slot):
        r1 = client.post("/turn", json={"session_id": s1, "text": "Yes, book it."})
        assert r1.status_code == 200
        assert f"Done! Your demo is booked for {slot_human}." in r1.json()["reply"]

        r2 = client.post("/turn", json={"session_id": s2, "text": "Yes, book it."})
        assert r2.status_code == 200
        assert slots[1] in r2.json()["reply"]
        assert slots[2] in r2.json()["reply"]

    # Also verify the partial unique index rejects a direct duplicate insert inside a transaction.
    conn = db.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert db.book_slot(lead_id=2, slot_datetime=target_slot, conn=conn) is False
        conn.rollback()
    finally:
        conn.close()


# Verify /end_call is idempotent: second call returns the saved summary without calling the LLM again.
def test_end_call_twice(client):
    session_id = client.post("/start_call", json={"phone": "+919800000003", "language": "en"}).json()["session_id"]

    mock_parsed_summary = {
        "summary": "Clinic asked about Zenvo Pro pricing.",
        "objections": ["Needs partner approval"],
        "buying_signals": ["Asked for demo"],
        "next_best_action": "Send pricing sheet.",
        "overall_sentiment": "positive",
        "final_score": 78,
        "sentiment_timeline": ["positive"],
    }

    with patch("app.summary.call_analyst_llm", return_value=mock_parsed_summary) as mock_llm:
        first = client.post("/end_call", json={"session_id": session_id})
        second = client.post("/end_call", json={"session_id": session_id})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert mock_llm.call_count == 1


# Verify calling /turn after a session has been ended returns HTTP 409 Conflict.
def test_turn_after_end(client):
    session_id = client.post("/start_call", json={"phone": "+919800000004", "language": "en"}).json()["session_id"]

    with patch("app.summary.call_analyst_llm", return_value=None):
        end_resp = client.post("/end_call", json={"session_id": session_id})
    assert end_resp.status_code == 200

    turn_resp = client.post("/turn", json={"session_id": session_id, "text": "Are you still there?"})
    assert turn_resp.status_code == 409


# Verify fallback responses never overwrite the lead's existing score or sentiment.
def test_fallback_does_not_change_lead(client):
    start_data = client.post("/start_call", json={"phone": "+919800000005", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    good_reply = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Rao", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=good_reply):
        r1 = client.post("/turn", json={"session_id": session_id, "text": "Tell me about Pro plan"})
        assert r1.status_code == 200

    lead_before = db.get_lead(lead_id)
    assert lead_before["score"] == 82
    assert lead_before["sentiment"] == "positive"

    fallback_result = {
        "reply": "Sorry, I had a brief connection issue. Let me have a human specialist follow up with you shortly.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "neutral",
        "lead_score": 20,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
        "fallback": True,
    }
    with patch("app.brain.generate_turn_response", return_value=fallback_result):
        r2 = client.post("/turn", json={"session_id": session_id, "text": "Hello?"})
        assert r2.status_code == 200

    lead_after = db.get_lead(lead_id)
    assert lead_after["score"] == 82
    assert lead_after["sentiment"] == "positive"
    assert lead_after["name"] == "Dr. Rao"


# Verify omitting language on /turn defaults to the session's stored language instead of 'en'.
def test_turn_uses_session_language_by_default(client):
    session_id = client.post("/start_call", json={"phone": "+919800000006", "language": "hi"}).json()["session_id"]

    captured = {}

    def fake_brain(lead, recent_messages, available_slots, user_text, language):
        captured["language"] = language
        return {
            "reply": "Haan ji, Zenvo Hindi support karta hai.",
            "language": language,
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": 50,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
            "fallback": False,
        }

    with patch("app.brain.generate_turn_response", side_effect=fake_brain):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Kya aap Hindi samajhte hain?"})

    assert resp.status_code == 200
    assert captured["language"] == "hi"
    assert resp.json()["language"] == "hi"


# Verify caller text exceeding 500 characters is rejected with HTTP 422.
def test_text_over_500_chars_returns_422(client):
    session_id = client.post("/start_call", json={"phone": "+919800000007", "language": "en"}).json()["session_id"]
    long_text = "a" * 501
    resp = client.post("/turn", json={"session_id": session_id, "text": long_text})
    assert resp.status_code == 422


# Verify failure inside save_turn rolls back the booking insert and returns HTTP 500.
def test_save_turn_error_rolls_back_booking(client, monkeypatch):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000008", "language": "en"}).json()["session_id"]

    propose_slot = {
        "reply": f"Shall I hold {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose_slot):
        assert client.post("/turn", json={"session_id": session_id, "text": "Propose a slot"}).status_code == 200

    def failing_save_turn(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "save_turn", failing_save_turn)

    confirm_slot = {
        "reply": "Confirming",
        "language": "en",
        "intent": "book_appointment",
        "sentiment": "positive",
        "lead_score": 90,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=confirm_slot):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Yes, book it"})

    assert resp.status_code == 500
    assert target_slot in client.get("/api/slots").json()["slots"]


# Verify two-step booking: turn 1 with no pending_slot does not book, turn 2 confirms with template, and question on new slot does not book.
def test_two_step_booking_confirmation(client):
    slots = client.get("/api/slots").json()["slots"]
    first_slot = slots[0]
    new_slot = slots[1]
    slot_human = handlers.format_slot_human(first_slot)

    session_id = client.post("/start_call", json={"phone": "+919800000009", "language": "en"}).json()["session_id"]

    # (a) First turn with intent=book_appointment but no pending_slot must NOT create an appointment.
    premature_book = {
        "reply": "Sure, booked!",
        "language": "en",
        "intent": "book_appointment",
        "sentiment": "positive",
        "lead_score": 80,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": first_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=premature_book):
        r1 = client.post("/turn", json={"session_id": session_id, "text": f"Book {first_slot}"})

    assert r1.status_code == 200
    assert r1.json()["intent"] == "enquiry"
    assert first_slot in client.get("/api/slots").json()["slots"]
    assert db.get_session(session_id)["pending_slot"] == first_slot

    # (c) A question on a NEW slot even with intent=book_appointment must NOT create an appointment.
    new_slot_question = {
        "reply": "Yes, let's check.",
        "language": "en",
        "intent": "book_appointment",
        "sentiment": "positive",
        "lead_score": 80,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": new_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=new_slot_question):
        r_new = client.post("/turn", json={"session_id": session_id, "text": "Is that slot available?"})

    assert r_new.status_code == 200
    assert r_new.json()["intent"] == "enquiry"
    assert new_slot in client.get("/api/slots").json()["slots"]

    # Restore first_slot as pending_slot via proposal turn, then (b) second turn confirms and books.
    with patch("app.brain.generate_turn_response", return_value=premature_book):
        client.post("/turn", json={"session_id": session_id, "text": f"Actually let's do {first_slot}"})

    with patch("app.brain.generate_turn_response", return_value=premature_book):
        r2 = client.post("/turn", json={"session_id": session_id, "text": "Yes, confirm it!"})

    assert r2.status_code == 200
    assert r2.json()["intent"] == "book_appointment"
    assert r2.json()["reply"] == f"Done! Your demo is booked for {slot_human}. You will get a confirmation shortly."
    assert first_slot not in client.get("/api/slots").json()["slots"]

    conn = db.get_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM appointments WHERE slot_datetime = ?", (first_slot,)).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()


# Verify overlapping /end_call handles IntegrityError on calls insert and returns existing summary.
def test_overlapping_end_call_returns_existing_summary(client):
    start_data = client.post("/start_call", json={"phone": "+919800000010", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    preexisting = {
        "summary": "Pre-inserted summary from concurrent request.",
        "objections": ["Pricing"],
        "buying_signals": ["Asked about trial"],
        "next_best_action": "Follow up tomorrow.",
        "overall_sentiment": "positive",
        "final_score": 85,
        "sentiment_timeline": ["positive"],
    }

    def insert_concurrent_call(transcript):
        conn = db.get_connection()
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
                    preexisting["summary"],
                    json.dumps(preexisting["objections"]),
                    json.dumps(preexisting["buying_signals"]),
                    preexisting["next_best_action"],
                    preexisting["overall_sentiment"],
                    preexisting["final_score"],
                    json.dumps(preexisting["sentiment_timeline"]),
                    db.now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "summary": "Second summary that hits IntegrityError.",
            "objections": [],
            "buying_signals": [],
            "next_best_action": "Nothing",
            "overall_sentiment": "neutral",
            "final_score": 40,
            "sentiment_timeline": ["neutral"],
        }

    with patch("app.summary.call_analyst_llm", side_effect=insert_concurrent_call):
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert resp.json() == preexisting


# Verify fallback turn + failed summary LLM preserves the lead's prior trusted score (82, not 20).
def test_fallback_turn_and_failed_summary_do_not_downgrade_lead(client):
    start_data = client.post("/start_call", json={"phone": "+919800000011", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    trusted_turn = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Sen", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=trusted_turn):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me about Pro"}).status_code == 200

    fallback_turn = {
        "reply": "Sorry, brief connection issue.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "neutral",
        "lead_score": 20,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
        "fallback": True,
    }
    with patch("app.brain.generate_turn_response", return_value=fallback_turn):
        assert client.post("/turn", json={"session_id": session_id, "text": "Hello?"}).status_code == 200

    with patch("app.summary.call_analyst_llm", return_value=None):
        end_resp = client.post("/end_call", json={"session_id": session_id})

    assert end_resp.status_code == 200
    assert "auto-generated (model unavailable)" in end_resp.json()["summary"]
    assert end_resp.json()["final_score"] == 82
    lead = db.get_lead(lead_id)
    assert lead["score"] == 82
    assert lead["sentiment"] == "positive"


# Verify legacy DB without sessions.started_at is backed up to zenvo.db.bak and recreated cleanly.
def test_legacy_schema_is_backed_up_on_init(tmp_path, monkeypatch):
    legacy_db = tmp_path / "zenvo.db"
    monkeypatch.setenv("DB_PATH", str(legacy_db))

    conn = sqlite3.connect(legacy_db)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, lead_id INTEGER, language TEXT)")
    conn.commit()
    conn.close()

    db.init_db()

    bak_path = tmp_path / "zenvo.db.bak"
    assert bak_path.exists()

    conn = db.get_connection()
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "started_at" in cols
        assert "pending_slot" in cols
    finally:
        conn.close()


# Build a mock RateLimitError with a tiny Retry-After header for fast deterministic tests.
def make_rate_limit_error() -> RateLimitError:
    resp = httpx.Response(
        429,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        headers={"Retry-After": "0.01"},
    )
    return RateLimitError("rate limited", response=resp, body=None)


# Build a mock OpenAI chat completion response object with JSON content.
def make_completion_response(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# Verify (a) primary 429 then success retries once and returns normal reply with fallback=False.
def test_rate_limit_retry_then_success(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000012", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    success_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 76,
        "extracted": {"name": "Dr. Nair", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[make_rate_limit_error(), make_completion_response(success_payload)],
    ) as mock_create:
        resp = client.post("/turn", json={"session_id": session_id, "text": "What is Pro price?"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Zenvo Pro is Rs 2,499 per month."
    assert resp.json()["lead_score"] == 76
    assert mock_create.call_count == 2
    lead = db.get_lead(lead_id)
    assert lead["score"] == 76
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Nair"


# Verify (b) repeated 429s trigger fallback=True and leave lead score and sentiment unchanged.
def test_rate_limit_both_fail_uses_fallback_and_preserves_lead(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000013", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    initial_payload = {
        "reply": "Zenvo Basic is Rs 999 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 80,
        "extracted": {"name": "Dr. Kapoor", "email": None, "budget": "999", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        return_value=make_completion_response(initial_payload),
    ):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me Basic price"}).status_code == 200

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[make_rate_limit_error(), make_rate_limit_error()],
    ) as mock_create:
        resp = client.post("/turn", json={"session_id": session_id, "text": "What about Enterprise?"})

    assert resp.status_code == 200
    assert "brief connection issue" in resp.json()["reply"]
    assert mock_create.call_count == 2

    lead = db.get_lead(lead_id)
    assert lead["score"] == 80
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Kapoor"


# Verify model returning unsupported language='pa' is coerced to the session's valid language.
def test_model_returning_pa_is_coerced_to_valid_language(client):
    session_id = client.post("/start_call", json={"phone": "+919800000014", "language": "hinglish"}).json()["session_id"]

    model_payload = {
        "reply": "Haan ji, Zenvo 14-day free trial deta hai.",
        "language": "pa",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 65,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
    }

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        return_value=make_completion_response(model_payload),
    ):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Kya free trial hai?"})

    assert resp.status_code == 200
    assert resp.json()["language"] == "hinglish"
    assert resp.json()["reply"] == "Haan ji, Zenvo 14-day free trial deta hai."

    saved_msgs = db.get_all_messages(session_id)
    assert all(msg["language"] in ("en", "hi", "hinglish") for msg in saved_msgs)
    assert saved_msgs[-1]["language"] == "hinglish"


# Build a mock OpenAI 503 InternalServerError or 401 AuthenticationError for secondary retry tests.
def make_http_error(status_code: int):
    resp = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    )
    if status_code == 401:
        return AuthenticationError("unauthorized", response=resp, body=None)
    return InternalServerError("high demand", response=resp, body=None)


# Build a mocked secondary (client, model) pair whose completions.create uses the given side_effect.
def make_secondary_mock(side_effect):
    sec_client = MagicMock()
    sec_client.chat.completions.create.side_effect = side_effect
    return sec_client, "gemini-3.5-flash-lite"


# Verify secondary turn path retries 503 -> 503 -> success (3 attempts, 1.0s sleep, reasoning_effort=low, json_object).
def test_secondary_turn_503_retries_then_succeeds(monkeypatch):
    turn_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    sec_client, sec_model = make_secondary_mock(
        [make_http_error(503), make_http_error(503), make_completion_response(turn_payload)]
    )
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))
    monkeypatch.setattr(brain.LLM_CLIENT.chat.completions, "create", MagicMock(side_effect=make_http_error(401)))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = brain.generate_turn_response(
            lead={},
            recent_messages=[],
            available_slots=["2026-11-26 10:00"],
            user_text="What is Pro pricing?",
            language="en",
        )

    assert result["fallback"] is False
    assert result["reply"] == "Zenvo Pro is Rs 2,499 per month."
    assert sec_client.chat.completions.create.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 1.0]
    first_kwargs = sec_client.chat.completions.create.call_args_list[0].kwargs
    assert first_kwargs["extra_body"] == {"reasoning_effort": "low"}
    assert first_kwargs["response_format"] == {"type": "json_object"}


# Verify secondary turn path stops after 3x 503 and returns the polite fallback (fallback=True).
def test_secondary_turn_503_exhausted_returns_fallback(monkeypatch):
    sec_client, sec_model = make_secondary_mock(
        [make_http_error(503), make_http_error(503), make_http_error(503), make_http_error(503)]
    )
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))
    monkeypatch.setattr(brain.LLM_CLIENT.chat.completions, "create", MagicMock(side_effect=make_http_error(401)))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = brain.generate_turn_response(
            lead={},
            recent_messages=[],
            available_slots=["2026-11-26 10:00"],
            user_text="What is Pro pricing?",
            language="en",
        )

    assert result["fallback"] is True
    assert sec_client.chat.completions.create.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 1.0]


# Verify secondary turn path stops immediately on 401 without retrying.
def test_secondary_turn_401_stops_immediately(monkeypatch):
    sec_client, sec_model = make_secondary_mock([make_http_error(401), make_http_error(503)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = brain.call_secondary_for_turn(
            chat_messages=[{"role": "user", "content": "Hi"}],
            language="en",
            deadline=time.monotonic() + 10.0,
        )

    assert result is None
    assert sec_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


# Verify reply-only LLM response (missing score/sentiment) falls back safely and does not downgrade lead.
def test_missing_score_sentiment_cannot_downgrade_lead(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000016", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    valid_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Mehta", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    reply_only_payload = {"reply": "Sure, happy to help."}

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[
            make_completion_response(valid_payload),
            make_completion_response(reply_only_payload),
            make_completion_response(reply_only_payload),
        ],
    ):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me Pro price"}).status_code == 200
        resp2 = client.post("/turn", json={"session_id": session_id, "text": "Okay thanks"}).json()

    assert "brief connection issue" in resp2["reply"]
    lead = db.get_lead(lead_id)
    assert lead["score"] == 82
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Mehta"


# Verify non-finite (1e999 / inf) and invalid (bool, string, out-of-range) scores never cause HTTP 500.
def test_invalid_and_non_finite_scores_never_500(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000017", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    db.merge_lead_fields(lead_id, score=75, sentiment="positive")

    for bad_score in (float("inf"), True, "ninety", -5, 150):
        bad_turn = {
            "reply": "Some reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": bad_score,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
        }
        with patch("app.brain.generate_turn_response", return_value=bad_turn):
            r = client.post("/turn", json={"session_id": session_id, "text": "Checking"})
        assert r.status_code == 200

    # Also test 1e999 raw JSON returned to /end_call summary parser.
    raw_msg = MagicMock()
    raw_msg.content = '{"summary":"Good call","overall_sentiment":"positive","final_score":1e999}'
    raw_choice = MagicMock()
    raw_choice.message = raw_msg
    raw_resp = MagicMock()
    raw_resp.choices = [raw_choice]

    with patch.object(summary.LLM_CLIENT.chat.completions, "create", return_value=raw_resp):
        end_resp = client.post("/end_call", json={"session_id": session_id})

    assert end_resp.status_code == 200
    assert "auto-generated (model unavailable)" in end_resp.json()["summary"]
    assert end_resp.json()["final_score"] == 75
    assert db.get_lead(lead_id)["score"] == 75


# Verify secondary provider rejects an overdue response when wall-clock deadline has elapsed.
def test_secondary_deadline_enforced_after_slow_call(monkeypatch):
    payload = {
        "reply": "Too late",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 60,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
    }
    sec_client, sec_model = make_secondary_mock([make_completion_response(payload)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    times = iter([100.0, 112.0])
    monkeypatch.setattr(brain.time, "monotonic", lambda: next(times, 112.0))

    result = brain.call_secondary_for_turn(
        chat_messages=[{"role": "user", "content": "Hi"}],
        language="en",
        deadline=110.0,
    )
    assert result is None


# Verify negative refusal ("No, please don't book that") cannot book and clears pending_slot.
def test_negative_booking_confirmation_clears_pending_slot(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000018", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        assert client.post("/turn", json={"session_id": session_id, "text": "Any slot?"}).status_code == 200
    assert db.get_session(session_id)["pending_slot"] == target_slot

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    with patch("app.brain.generate_turn_response", return_value=false_book):
        resp = client.post("/turn", json={"session_id": session_id, "text": "No, please don't book that"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "enquiry"
    assert target_slot in client.get("/api/slots").json()["slots"]
    assert db.get_session(session_id)["pending_slot"] is None


# Verify ambiguous text ("Maybe tomorrow?" or "Yesterday I checked") cannot book even with book_appointment intent.
def test_ambiguous_confirmation_cannot_book(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000019", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Any slot?"})

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    for ambiguous_text in ("Maybe tomorrow?", "Yesterday I checked my diary"):
        with patch("app.brain.generate_turn_response", return_value=false_book):
            resp = client.post("/turn", json={"session_id": session_id, "text": ambiguous_text})
        assert resp.status_code == 200
        assert resp.json()["intent"] == "enquiry"
        assert target_slot in client.get("/api/slots").json()["slots"]

    # Now explicit affirmative confirmation still books cleanly.
    with patch("app.brain.generate_turn_response", return_value=false_book):
        confirm_resp = client.post("/turn", json={"session_id": session_id, "text": "Haan ji, book kar do"})
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["intent"] == "book_appointment"
    assert target_slot not in client.get("/api/slots").json()["slots"]


# Verify a turn cannot commit after /end_call ends the session mid-flight (returns 409).
def test_turn_cannot_commit_after_session_ended_mid_flight(client):
    session_id = client.post("/start_call", json={"phone": "+919800000020", "language": "en"}).json()["session_id"]
    before_count = len(db.get_all_messages(session_id))

    def end_session_during_llm(*args, **kwargs):
        with patch("app.summary.call_analyst_llm", return_value=None):
            summary.generate_and_save_summary(session_id, db.get_session(session_id)["lead_id"])
        return {
            "reply": "Late reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": 80,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
            "fallback": False,
        }

    with patch("app.brain.generate_turn_response", side_effect=end_session_during_llm):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Hello?"})

    assert resp.status_code == 409
    assert len(db.get_all_messages(session_id)) == before_count


# Verify summary refreshes if a concurrent turn commits while the summary LLM is running.
def test_summary_refreshes_if_concurrent_turn_committed(client):
    start_data = client.post("/start_call", json={"phone": "+919800000021", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    calls_seen = []

    def concurrent_analyst(transcript):
        calls_seen.append(transcript)
        if len(calls_seen) == 1:
            db.save_message(session_id, lead_id, "user", "Actually book Pro plan!", "en", "positive", 90)
            return {
                "summary": "Stale summary",
                "objections": [],
                "buying_signals": [],
                "next_best_action": "None",
                "overall_sentiment": "neutral",
                "final_score": 40,
                "sentiment_timeline": ["neutral"],
            }
        return {
            "summary": "Updated summary with Pro plan booking.",
            "objections": [],
            "buying_signals": ["Book Pro plan"],
            "next_best_action": "Send invoice",
            "overall_sentiment": "positive",
            "final_score": 90,
            "sentiment_timeline": ["positive"],
        }

    with patch("app.summary.call_analyst_llm", side_effect=concurrent_analyst):
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert len(calls_seen) == 2
    assert "Actually book Pro plan!" in calls_seen[1]
    assert resp.json()["final_score"] == 90


# Verify unsupported API language 'pa' is rejected with 422 on /start_call and /turn.
def test_unsupported_api_language_rejected(client):
    bad_start = client.post("/start_call", json={"phone": "+919800000022", "language": "pa"})
    assert bad_start.status_code == 422

    session_id = client.post("/start_call", json={"phone": "+919800000022", "language": "en"}).json()["session_id"]
    bad_turn = client.post("/turn", json={"session_id": session_id, "text": "Hello", "language": "pa"})
    assert bad_turn.status_code == 422


# Verify scripted booking confirmation returns the session's reply language even if LLM tagged another language.
def test_booking_confirmation_returns_matching_reply_language(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000023", "language": "hi"}).json()["session_id"]

    propose = {
        "reply": f"Kya main {target_slot} book karoon?",
        "language": "hinglish",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Demo kab hai?"})

    confirm_mismatched_lang = dict(propose, intent="book_appointment", language="hinglish")
    with patch("app.brain.generate_turn_response", return_value=confirm_mismatched_lang):
        resp = client.post("/turn", json={"session_id": session_id, "text": "हाँ, बुक कर दो"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "book_appointment"
    assert resp.json()["language"] == "hi"
    assert "आपका डेमो" in resp.json()["reply"]


# Verify GET / and both static assets return HTTP 200.
def test_frontend_routes_return_200(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/call.css").status_code == 200
    assert client.get("/static/call.js").status_code == 200


# Verify empty database dashboard renders HTTP 200 with zeroed KPIs and empty-state messages.
def test_empty_dashboard_renders_cleanly(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert client.get("/static/dashboard.css").status_code == 200
    html = resp.text
    assert "0.0%" in html
    assert "0 booked / 0 total leads" in html
    assert "No leads recorded yet." in html
    assert "No appointments booked yet." in html
    assert "No completed calls yet." in html


# Verify seeded dashboard KPIs, masked phones, and call detail link match seeded records.
def test_dashboard_kpis_and_masked_phones_with_seed_data(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Seed has 3 leads (scores 88, 72, 48 -> avg 69.3), 3 calls, 3 qualified, 1 booked (33.3%)
    assert "69.3" in html
    assert "33.3%" in html
    assert "1 booked / 3 total leads" in html
    assert "Dr. Ananya Sharma" in html
    assert "Dr. Harpreet Gill" in html
    assert "/dashboard/calls/1" in html

    # Raw phone numbers must never appear in HTML; masked numbers must appear instead.
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "+919811223344" not in html
    assert "+919822334455" not in html
    assert "+91******3210" in html


# Verify existing call details page returns 200 with transcript, timeline, masked phone, and back link.
def test_call_detail_page_renders_seeded_call(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard/calls/1")
    assert resp.status_code == 200
    html = resp.text

    assert "Dr. Ananya Sharma" in html
    assert "+91******3210" in html
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "Wanted to confirm patient data stays in India" in html
    assert "Booked an onboarding demo slot" in html
    assert "Back to Dashboard" in html
    assert 'href="/dashboard"' in html
    assert "Step 1" in html


# Verify nonexistent call ID on /dashboard/calls/{call_id} returns HTTP 404.
def test_call_detail_nonexistent_returns_404(client):
    resp = client.get("/dashboard/calls/9999")
    assert resp.status_code == 404


# Verify customer-supplied and LLM-generated HTML/script tags are escaped on both dashboard pages.
def test_dashboard_escapes_customer_and_llm_html(client):
    xss_name = "<script>alert('name_xss')</script>"
    xss_need = "<img src=x onerror=alert('need_xss')>"
    xss_summary = "<script>alert('summary_xss')</script>"
    xss_msg = "<svg onload=alert('msg_xss')>"
    raw_phone = "+919877665544"

    start_data = client.post("/start_call", json={"phone": raw_phone, "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    turn_payload = {
        "reply": f"Noted {xss_msg}",
        "language": "en",
        "intent": "update_crm",
        "sentiment": "positive",
        "lead_score": 65,
        "extracted": {"name": xss_name, "email": None, "budget": None, "need": xss_need, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=turn_payload):
        assert client.post("/turn", json={"session_id": session_id, "text": xss_msg}).status_code == 200

    summary_payload = {
        "summary": xss_summary,
        "objections": ["<b>No</b>"],
        "buying_signals": ["<i>Yes</i>"],
        "next_best_action": "<u>Call back</u>",
        "overall_sentiment": "positive",
        "final_score": 65,
        "sentiment_timeline": ["positive"],
    }
    with patch("app.summary.call_analyst_llm", return_value=summary_payload):
        assert client.post("/end_call", json={"session_id": session_id}).status_code == 200

    dash_html = client.get("/dashboard").text
    assert raw_phone not in dash_html
    assert "+91******5544" in dash_html
    assert xss_name not in dash_html
    assert "&lt;script&gt;alert(&#39;name_xss&#39;)&lt;/script&gt;" in dash_html
    assert xss_need not in dash_html
    assert "&lt;img src=x onerror=alert(&#39;need_xss&#39;)&gt;" in dash_html

    detail_html = client.get("/dashboard/calls/1").text
    assert raw_phone not in detail_html
    assert "+91******5544" in detail_html
    assert xss_summary not in detail_html
    assert "&lt;script&gt;alert(&#39;summary_xss&#39;)&lt;/script&gt;" in detail_html
    assert xss_msg not in detail_html
    assert "&lt;svg onload=alert(&#39;msg_xss&#39;)&gt;" in detail_html


# Verify secondary turn path skips retry when remaining time budget is insufficient.
def test_secondary_turn_insufficient_budget_stops_without_retry(monkeypatch):
    sec_client, sec_model = make_secondary_mock([make_http_error(503), make_http_error(503)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = brain.call_secondary_for_turn(
            chat_messages=[{"role": "user", "content": "Hi"}],
            language="en",
            deadline=time.monotonic() + 0.5,
        )

    assert result is None
    assert sec_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


# Verify reply-only LLM response (missing score/sentiment) falls back safely and does not downgrade lead.
def test_missing_score_sentiment_cannot_downgrade_lead(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000016", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    valid_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Mehta", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    reply_only_payload = {"reply": "Sure, happy to help."}

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[
            make_completion_response(valid_payload),
            make_completion_response(reply_only_payload),
            make_completion_response(reply_only_payload),
        ],
    ):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me Pro price"}).status_code == 200
        resp2 = client.post("/turn", json={"session_id": session_id, "text": "Okay thanks"}).json()

    assert "brief connection issue" in resp2["reply"]
    lead = db.get_lead(lead_id)
    assert lead["score"] == 82
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Mehta"


# Verify non-finite (1e999 / inf) and invalid (bool, string, out-of-range) scores never cause HTTP 500.
def test_invalid_and_non_finite_scores_never_500(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000017", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    db.merge_lead_fields(lead_id, score=75, sentiment="positive")

    for bad_score in (float("inf"), True, "ninety", -5, 150):
        bad_turn = {
            "reply": "Some reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": bad_score,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
        }
        with patch("app.brain.generate_turn_response", return_value=bad_turn):
            r = client.post("/turn", json={"session_id": session_id, "text": "Checking"})
        assert r.status_code == 200

    # Also test 1e999 raw JSON returned to /end_call summary parser.
    raw_msg = MagicMock()
    raw_msg.content = '{"summary":"Good call","overall_sentiment":"positive","final_score":1e999}'
    raw_choice = MagicMock()
    raw_choice.message = raw_msg
    raw_resp = MagicMock()
    raw_resp.choices = [raw_choice]

    with patch.object(summary.LLM_CLIENT.chat.completions, "create", return_value=raw_resp):
        end_resp = client.post("/end_call", json={"session_id": session_id})

    assert end_resp.status_code == 200
    assert "auto-generated (model unavailable)" in end_resp.json()["summary"]
    assert end_resp.json()["final_score"] == 75
    assert db.get_lead(lead_id)["score"] == 75


# Verify secondary provider rejects an overdue response when wall-clock deadline has elapsed.
def test_secondary_deadline_enforced_after_slow_call(monkeypatch):
    payload = {
        "reply": "Too late",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 60,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
    }
    sec_client, sec_model = make_secondary_mock([make_completion_response(payload)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    times = iter([100.0, 112.0])
    monkeypatch.setattr(brain.time, "monotonic", lambda: next(times, 112.0))

    result = brain.call_secondary_for_turn(
        chat_messages=[{"role": "user", "content": "Hi"}],
        language="en",
        deadline=110.0,
    )
    assert result is None


# Verify negative refusal ("No, please don't book that") cannot book and clears pending_slot.
def test_negative_booking_confirmation_clears_pending_slot(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000018", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        assert client.post("/turn", json={"session_id": session_id, "text": "Any slot?"}).status_code == 200
    assert db.get_session(session_id)["pending_slot"] == target_slot

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    with patch("app.brain.generate_turn_response", return_value=false_book):
        resp = client.post("/turn", json={"session_id": session_id, "text": "No, please don't book that"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "enquiry"
    assert target_slot in client.get("/api/slots").json()["slots"]
    assert db.get_session(session_id)["pending_slot"] is None


# Verify ambiguous text ("Maybe tomorrow?" or "Yesterday I checked") cannot book even with book_appointment intent.
def test_ambiguous_confirmation_cannot_book(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000019", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Any slot?"})

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    for ambiguous_text in ("Maybe tomorrow?", "Yesterday I checked my diary"):
        with patch("app.brain.generate_turn_response", return_value=false_book):
            resp = client.post("/turn", json={"session_id": session_id, "text": ambiguous_text})
        assert resp.status_code == 200
        assert resp.json()["intent"] == "enquiry"
        assert target_slot in client.get("/api/slots").json()["slots"]

    # Now explicit affirmative confirmation still books cleanly.
    with patch("app.brain.generate_turn_response", return_value=false_book):
        confirm_resp = client.post("/turn", json={"session_id": session_id, "text": "Haan ji, book kar do"})
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["intent"] == "book_appointment"
    assert target_slot not in client.get("/api/slots").json()["slots"]


# Verify a turn cannot commit after /end_call ends the session mid-flight (returns 409).
def test_turn_cannot_commit_after_session_ended_mid_flight(client):
    session_id = client.post("/start_call", json={"phone": "+919800000020", "language": "en"}).json()["session_id"]
    before_count = len(db.get_all_messages(session_id))

    def end_session_during_llm(*args, **kwargs):
        with patch("app.summary.call_analyst_llm", return_value=None):
            summary.generate_and_save_summary(session_id, db.get_session(session_id)["lead_id"])
        return {
            "reply": "Late reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": 80,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
            "fallback": False,
        }

    with patch("app.brain.generate_turn_response", side_effect=end_session_during_llm):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Hello?"})

    assert resp.status_code == 409
    assert len(db.get_all_messages(session_id)) == before_count


# Verify summary refreshes if a concurrent turn commits while the summary LLM is running.
def test_summary_refreshes_if_concurrent_turn_committed(client):
    start_data = client.post("/start_call", json={"phone": "+919800000021", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    calls_seen = []

    def concurrent_analyst(transcript):
        calls_seen.append(transcript)
        if len(calls_seen) == 1:
            db.save_message(session_id, lead_id, "user", "Actually book Pro plan!", "en", "positive", 90)
            return {
                "summary": "Stale summary",
                "objections": [],
                "buying_signals": [],
                "next_best_action": "None",
                "overall_sentiment": "neutral",
                "final_score": 40,
                "sentiment_timeline": ["neutral"],
            }
        return {
            "summary": "Updated summary with Pro plan booking.",
            "objections": [],
            "buying_signals": ["Book Pro plan"],
            "next_best_action": "Send invoice",
            "overall_sentiment": "positive",
            "final_score": 90,
            "sentiment_timeline": ["positive"],
        }

    with patch("app.summary.call_analyst_llm", side_effect=concurrent_analyst):
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert len(calls_seen) == 2
    assert "Actually book Pro plan!" in calls_seen[1]
    assert resp.json()["final_score"] == 90


# Verify unsupported API language 'pa' is rejected with 422 on /start_call and /turn.
def test_unsupported_api_language_rejected(client):
    bad_start = client.post("/start_call", json={"phone": "+919800000022", "language": "pa"})
    assert bad_start.status_code == 422

    session_id = client.post("/start_call", json={"phone": "+919800000022", "language": "en"}).json()["session_id"]
    bad_turn = client.post("/turn", json={"session_id": session_id, "text": "Hello", "language": "pa"})
    assert bad_turn.status_code == 422


# Verify scripted booking confirmation returns the session's reply language even if LLM tagged another language.
def test_booking_confirmation_returns_matching_reply_language(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000023", "language": "hi"}).json()["session_id"]

    propose = {
        "reply": f"Kya main {target_slot} book karoon?",
        "language": "hinglish",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Demo kab hai?"})

    confirm_mismatched_lang = dict(propose, intent="book_appointment", language="hinglish")
    with patch("app.brain.generate_turn_response", return_value=confirm_mismatched_lang):
        resp = client.post("/turn", json={"session_id": session_id, "text": "हाँ, बुक कर दो"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "book_appointment"
    assert resp.json()["language"] == "hi"
    assert "आपका डेमो" in resp.json()["reply"]


# Verify GET / and both static assets return HTTP 200.
def test_frontend_routes_return_200(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/call.css").status_code == 200
    assert client.get("/static/call.js").status_code == 200


# Verify empty database dashboard renders HTTP 200 with zeroed KPIs and empty-state messages.
def test_empty_dashboard_renders_cleanly(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert client.get("/static/dashboard.css").status_code == 200
    html = resp.text
    assert "0.0%" in html
    assert "0 booked / 0 total leads" in html
    assert "No leads recorded yet." in html
    assert "No appointments booked yet." in html
    assert "No completed calls yet." in html


# Verify seeded dashboard KPIs, masked phones, and call detail link match seeded records.
def test_dashboard_kpis_and_masked_phones_with_seed_data(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Seed has 3 leads (scores 88, 72, 48 -> avg 69.3), 3 calls, 3 qualified, 1 booked (33.3%)
    assert "69.3" in html
    assert "33.3%" in html
    assert "1 booked / 3 total leads" in html
    assert "Dr. Ananya Sharma" in html
    assert "Dr. Harpreet Gill" in html
    assert "/dashboard/calls/1" in html

    # Raw phone numbers must never appear in HTML; masked numbers must appear instead.
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "+919811223344" not in html
    assert "+919822334455" not in html
    assert "+91******3210" in html


# Verify existing call details page returns 200 with transcript, timeline, masked phone, and back link.
def test_call_detail_page_renders_seeded_call(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard/calls/1")
    assert resp.status_code == 200
    html = resp.text

    assert "Dr. Ananya Sharma" in html
    assert "+91******3210" in html
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "Wanted to confirm patient data stays in India" in html
    assert "Booked an onboarding demo slot" in html
    assert "Back to Dashboard" in html
    assert 'href="/dashboard"' in html
    assert "Step 1" in html


# Verify nonexistent call ID on /dashboard/calls/{call_id} returns HTTP 404.
def test_call_detail_nonexistent_returns_404(client):
    resp = client.get("/dashboard/calls/9999")
    assert resp.status_code == 404


# Verify customer-supplied and LLM-generated HTML/script tags are escaped on both dashboard pages.
def test_dashboard_escapes_customer_and_llm_html(client):
    xss_name = "<script>alert('name_xss')</script>"
    xss_need = "<img src=x onerror=alert('need_xss')>"
    xss_summary = "<script>alert('summary_xss')</script>"
    xss_msg = "<svg onload=alert('msg_xss')>"
    raw_phone = "+919877665544"

    start_data = client.post("/start_call", json={"phone": raw_phone, "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    turn_payload = {
        "reply": f"Noted {xss_msg}",
        "language": "en",
        "intent": "update_crm",
        "sentiment": "positive",
        "lead_score": 65,
        "extracted": {"name": xss_name, "email": None, "budget": None, "need": xss_need, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=turn_payload):
        assert client.post("/turn", json={"session_id": session_id, "text": xss_msg}).status_code == 200

    summary_payload = {
        "summary": xss_summary,
        "objections": ["<b>No</b>"],
        "buying_signals": ["<i>Yes</i>"],
        "next_best_action": "<u>Call back</u>",
        "overall_sentiment": "positive",
        "final_score": 65,
        "sentiment_timeline": ["positive"],
    }
    with patch("app.summary.call_analyst_llm", return_value=summary_payload):
        assert client.post("/end_call", json={"session_id": session_id}).status_code == 200

    dash_html = client.get("/dashboard").text
    assert raw_phone not in dash_html
    assert "+91******5544" in dash_html
    assert xss_name not in dash_html
    assert "&lt;script&gt;alert(&#39;name_xss&#39;)&lt;/script&gt;" in dash_html
    assert xss_need not in dash_html
    assert "&lt;img src=x onerror=alert(&#39;need_xss&#39;)&gt;" in dash_html

    detail_html = client.get("/dashboard/calls/1").text
    assert raw_phone not in detail_html
    assert "+91******5544" in detail_html
    assert xss_summary not in detail_html
    assert "&lt;script&gt;alert(&#39;summary_xss&#39;)&lt;/script&gt;" in detail_html
    assert xss_msg not in detail_html
    assert "&lt;svg onload=alert(&#39;msg_xss&#39;)&gt;" in detail_html


# Verify secondary summary path retries 503 -> 503 -> success (3 attempts, 1.0s sleep).
def test_secondary_summary_503_retries_then_succeeds(monkeypatch):
    summary_payload = {
        "summary": "Lead asked about Pro plan.",
        "objections": [],
        "buying_signals": ["Asked price"],
        "next_best_action": "Follow up tomorrow.",
        "overall_sentiment": "positive",
        "final_score": 75,
        "sentiment_timeline": ["positive"],
    }
    sec_client, sec_model = make_secondary_mock(
        [make_http_error(503), make_http_error(503), make_completion_response(summary_payload)]
    )
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))
    monkeypatch.setattr(summary.LLM_CLIENT.chat.completions, "create", MagicMock(side_effect=make_http_error(401)))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = summary.call_analyst_llm("USER: What is Pro pricing?")

    assert result == summary_payload
    assert sec_client.chat.completions.create.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 1.0]
    first_kwargs = sec_client.chat.completions.create.call_args_list[0].kwargs
    assert first_kwargs["extra_body"] == {"reasoning_effort": "low"}
    assert first_kwargs["response_format"] == {"type": "json_object"}


# Verify secondary summary path stops after 3x 503 and falls back to stored-lead summary.
def test_secondary_summary_503_exhausted_returns_fallback_summary(client, monkeypatch):
    start_data = client.post("/start_call", json={"phone": "+919800000015", "language": "en"}).json()
    session_id = start_data["session_id"]

    sec_client, sec_model = make_secondary_mock(
        [make_http_error(503), make_http_error(503), make_http_error(503), make_http_error(503)]
    )
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))
    monkeypatch.setattr(summary.LLM_CLIENT.chat.completions, "create", MagicMock(side_effect=make_http_error(401)))

    with patch("app.brain.time.sleep") as mock_sleep:
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert "auto-generated (model unavailable)" in resp.json()["summary"]
    assert sec_client.chat.completions.create.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 1.0]


# Verify secondary summary path stops immediately on 401 without retrying.
def test_secondary_summary_401_stops_immediately(monkeypatch):
    sec_client, sec_model = make_secondary_mock([make_http_error(401), make_http_error(503)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = summary.call_secondary_for_summary(
            chat_messages=[{"role": "user", "content": "Transcript"}],
            deadline=time.monotonic() + 20.0,
        )

    assert result is None
    assert sec_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


# Verify reply-only LLM response (missing score/sentiment) falls back safely and does not downgrade lead.
def test_missing_score_sentiment_cannot_downgrade_lead(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000016", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    valid_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Mehta", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    reply_only_payload = {"reply": "Sure, happy to help."}

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[
            make_completion_response(valid_payload),
            make_completion_response(reply_only_payload),
            make_completion_response(reply_only_payload),
        ],
    ):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me Pro price"}).status_code == 200
        resp2 = client.post("/turn", json={"session_id": session_id, "text": "Okay thanks"}).json()

    assert "brief connection issue" in resp2["reply"]
    lead = db.get_lead(lead_id)
    assert lead["score"] == 82
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Mehta"


# Verify non-finite (1e999 / inf) and invalid (bool, string, out-of-range) scores never cause HTTP 500.
def test_invalid_and_non_finite_scores_never_500(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000017", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    db.merge_lead_fields(lead_id, score=75, sentiment="positive")

    for bad_score in (float("inf"), True, "ninety", -5, 150):
        bad_turn = {
            "reply": "Some reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": bad_score,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
        }
        with patch("app.brain.generate_turn_response", return_value=bad_turn):
            r = client.post("/turn", json={"session_id": session_id, "text": "Checking"})
        assert r.status_code == 200

    # Also test 1e999 raw JSON returned to /end_call summary parser.
    raw_msg = MagicMock()
    raw_msg.content = '{"summary":"Good call","overall_sentiment":"positive","final_score":1e999}'
    raw_choice = MagicMock()
    raw_choice.message = raw_msg
    raw_resp = MagicMock()
    raw_resp.choices = [raw_choice]

    with patch.object(summary.LLM_CLIENT.chat.completions, "create", return_value=raw_resp):
        end_resp = client.post("/end_call", json={"session_id": session_id})

    assert end_resp.status_code == 200
    assert "auto-generated (model unavailable)" in end_resp.json()["summary"]
    assert end_resp.json()["final_score"] == 75
    assert db.get_lead(lead_id)["score"] == 75


# Verify secondary provider rejects an overdue response when wall-clock deadline has elapsed.
def test_secondary_deadline_enforced_after_slow_call(monkeypatch):
    payload = {
        "reply": "Too late",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 60,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
    }
    sec_client, sec_model = make_secondary_mock([make_completion_response(payload)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    times = iter([100.0, 112.0])
    monkeypatch.setattr(brain.time, "monotonic", lambda: next(times, 112.0))

    result = brain.call_secondary_for_turn(
        chat_messages=[{"role": "user", "content": "Hi"}],
        language="en",
        deadline=110.0,
    )
    assert result is None


# Verify negative refusal ("No, please don't book that") cannot book and clears pending_slot.
def test_negative_booking_confirmation_clears_pending_slot(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000018", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        assert client.post("/turn", json={"session_id": session_id, "text": "Any slot?"}).status_code == 200
    assert db.get_session(session_id)["pending_slot"] == target_slot

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    with patch("app.brain.generate_turn_response", return_value=false_book):
        resp = client.post("/turn", json={"session_id": session_id, "text": "No, please don't book that"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "enquiry"
    assert target_slot in client.get("/api/slots").json()["slots"]
    assert db.get_session(session_id)["pending_slot"] is None


# Verify ambiguous text ("Maybe tomorrow?" or "Yesterday I checked") cannot book even with book_appointment intent.
def test_ambiguous_confirmation_cannot_book(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000019", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Any slot?"})

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    for ambiguous_text in ("Maybe tomorrow?", "Yesterday I checked my diary"):
        with patch("app.brain.generate_turn_response", return_value=false_book):
            resp = client.post("/turn", json={"session_id": session_id, "text": ambiguous_text})
        assert resp.status_code == 200
        assert resp.json()["intent"] == "enquiry"
        assert target_slot in client.get("/api/slots").json()["slots"]

    # Now explicit affirmative confirmation still books cleanly.
    with patch("app.brain.generate_turn_response", return_value=false_book):
        confirm_resp = client.post("/turn", json={"session_id": session_id, "text": "Haan ji, book kar do"})
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["intent"] == "book_appointment"
    assert target_slot not in client.get("/api/slots").json()["slots"]


# Verify a turn cannot commit after /end_call ends the session mid-flight (returns 409).
def test_turn_cannot_commit_after_session_ended_mid_flight(client):
    session_id = client.post("/start_call", json={"phone": "+919800000020", "language": "en"}).json()["session_id"]
    before_count = len(db.get_all_messages(session_id))

    def end_session_during_llm(*args, **kwargs):
        with patch("app.summary.call_analyst_llm", return_value=None):
            summary.generate_and_save_summary(session_id, db.get_session(session_id)["lead_id"])
        return {
            "reply": "Late reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": 80,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
            "fallback": False,
        }

    with patch("app.brain.generate_turn_response", side_effect=end_session_during_llm):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Hello?"})

    assert resp.status_code == 409
    assert len(db.get_all_messages(session_id)) == before_count


# Verify summary refreshes if a concurrent turn commits while the summary LLM is running.
def test_summary_refreshes_if_concurrent_turn_committed(client):
    start_data = client.post("/start_call", json={"phone": "+919800000021", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    calls_seen = []

    def concurrent_analyst(transcript):
        calls_seen.append(transcript)
        if len(calls_seen) == 1:
            db.save_message(session_id, lead_id, "user", "Actually book Pro plan!", "en", "positive", 90)
            return {
                "summary": "Stale summary",
                "objections": [],
                "buying_signals": [],
                "next_best_action": "None",
                "overall_sentiment": "neutral",
                "final_score": 40,
                "sentiment_timeline": ["neutral"],
            }
        return {
            "summary": "Updated summary with Pro plan booking.",
            "objections": [],
            "buying_signals": ["Book Pro plan"],
            "next_best_action": "Send invoice",
            "overall_sentiment": "positive",
            "final_score": 90,
            "sentiment_timeline": ["positive"],
        }

    with patch("app.summary.call_analyst_llm", side_effect=concurrent_analyst):
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert len(calls_seen) == 2
    assert "Actually book Pro plan!" in calls_seen[1]
    assert resp.json()["final_score"] == 90


# Verify unsupported API language 'pa' is rejected with 422 on /start_call and /turn.
def test_unsupported_api_language_rejected(client):
    bad_start = client.post("/start_call", json={"phone": "+919800000022", "language": "pa"})
    assert bad_start.status_code == 422

    session_id = client.post("/start_call", json={"phone": "+919800000022", "language": "en"}).json()["session_id"]
    bad_turn = client.post("/turn", json={"session_id": session_id, "text": "Hello", "language": "pa"})
    assert bad_turn.status_code == 422


# Verify scripted booking confirmation returns the session's reply language even if LLM tagged another language.
def test_booking_confirmation_returns_matching_reply_language(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000023", "language": "hi"}).json()["session_id"]

    propose = {
        "reply": f"Kya main {target_slot} book karoon?",
        "language": "hinglish",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Demo kab hai?"})

    confirm_mismatched_lang = dict(propose, intent="book_appointment", language="hinglish")
    with patch("app.brain.generate_turn_response", return_value=confirm_mismatched_lang):
        resp = client.post("/turn", json={"session_id": session_id, "text": "हाँ, बुक कर दो"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "book_appointment"
    assert resp.json()["language"] == "hi"
    assert "आपका डेमो" in resp.json()["reply"]


# Verify GET / and both static assets return HTTP 200.
def test_frontend_routes_return_200(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/call.css").status_code == 200
    assert client.get("/static/call.js").status_code == 200


# Verify empty database dashboard renders HTTP 200 with zeroed KPIs and empty-state messages.
def test_empty_dashboard_renders_cleanly(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert client.get("/static/dashboard.css").status_code == 200
    html = resp.text
    assert "0.0%" in html
    assert "0 booked / 0 total leads" in html
    assert "No leads recorded yet." in html
    assert "No appointments booked yet." in html
    assert "No completed calls yet." in html


# Verify seeded dashboard KPIs, masked phones, and call detail link match seeded records.
def test_dashboard_kpis_and_masked_phones_with_seed_data(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Seed has 3 leads (scores 88, 72, 48 -> avg 69.3), 3 calls, 3 qualified, 1 booked (33.3%)
    assert "69.3" in html
    assert "33.3%" in html
    assert "1 booked / 3 total leads" in html
    assert "Dr. Ananya Sharma" in html
    assert "Dr. Harpreet Gill" in html
    assert "/dashboard/calls/1" in html

    # Raw phone numbers must never appear in HTML; masked numbers must appear instead.
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "+919811223344" not in html
    assert "+919822334455" not in html
    assert "+91******3210" in html


# Verify existing call details page returns 200 with transcript, timeline, masked phone, and back link.
def test_call_detail_page_renders_seeded_call(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard/calls/1")
    assert resp.status_code == 200
    html = resp.text

    assert "Dr. Ananya Sharma" in html
    assert "+91******3210" in html
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "Wanted to confirm patient data stays in India" in html
    assert "Booked an onboarding demo slot" in html
    assert "Back to Dashboard" in html
    assert 'href="/dashboard"' in html
    assert "Step 1" in html


# Verify nonexistent call ID on /dashboard/calls/{call_id} returns HTTP 404.
def test_call_detail_nonexistent_returns_404(client):
    resp = client.get("/dashboard/calls/9999")
    assert resp.status_code == 404


# Verify customer-supplied and LLM-generated HTML/script tags are escaped on both dashboard pages.
def test_dashboard_escapes_customer_and_llm_html(client):
    xss_name = "<script>alert('name_xss')</script>"
    xss_need = "<img src=x onerror=alert('need_xss')>"
    xss_summary = "<script>alert('summary_xss')</script>"
    xss_msg = "<svg onload=alert('msg_xss')>"
    raw_phone = "+919877665544"

    start_data = client.post("/start_call", json={"phone": raw_phone, "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    turn_payload = {
        "reply": f"Noted {xss_msg}",
        "language": "en",
        "intent": "update_crm",
        "sentiment": "positive",
        "lead_score": 65,
        "extracted": {"name": xss_name, "email": None, "budget": None, "need": xss_need, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=turn_payload):
        assert client.post("/turn", json={"session_id": session_id, "text": xss_msg}).status_code == 200

    summary_payload = {
        "summary": xss_summary,
        "objections": ["<b>No</b>"],
        "buying_signals": ["<i>Yes</i>"],
        "next_best_action": "<u>Call back</u>",
        "overall_sentiment": "positive",
        "final_score": 65,
        "sentiment_timeline": ["positive"],
    }
    with patch("app.summary.call_analyst_llm", return_value=summary_payload):
        assert client.post("/end_call", json={"session_id": session_id}).status_code == 200

    dash_html = client.get("/dashboard").text
    assert raw_phone not in dash_html
    assert "+91******5544" in dash_html
    assert xss_name not in dash_html
    assert "&lt;script&gt;alert(&#39;name_xss&#39;)&lt;/script&gt;" in dash_html
    assert xss_need not in dash_html
    assert "&lt;img src=x onerror=alert(&#39;need_xss&#39;)&gt;" in dash_html

    detail_html = client.get("/dashboard/calls/1").text
    assert raw_phone not in detail_html
    assert "+91******5544" in detail_html
    assert xss_summary not in detail_html
    assert "&lt;script&gt;alert(&#39;summary_xss&#39;)&lt;/script&gt;" in detail_html
    assert xss_msg not in detail_html
    assert "&lt;svg onload=alert(&#39;msg_xss&#39;)&gt;" in detail_html


# Verify secondary summary path skips retry when remaining time budget is insufficient.
def test_secondary_summary_insufficient_budget_stops_without_retry(monkeypatch):
    sec_client, sec_model = make_secondary_mock([make_http_error(503), make_http_error(503)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    with patch("app.brain.time.sleep") as mock_sleep:
        result = summary.call_secondary_for_summary(
            chat_messages=[{"role": "user", "content": "Transcript"}],
            deadline=time.monotonic() + 0.5,
        )

    assert result is None
    assert sec_client.chat.completions.create.call_count == 1
    mock_sleep.assert_not_called()


# Verify reply-only LLM response (missing score/sentiment) falls back safely and does not downgrade lead.
def test_missing_score_sentiment_cannot_downgrade_lead(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000016", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    valid_payload = {
        "reply": "Zenvo Pro is Rs 2,499 per month.",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 82,
        "extracted": {"name": "Dr. Mehta", "email": None, "budget": "2499", "need": None, "timeline": None},
        "slot_datetime": None,
    }
    reply_only_payload = {"reply": "Sure, happy to help."}

    with patch.object(
        brain.LLM_CLIENT.chat.completions,
        "create",
        side_effect=[
            make_completion_response(valid_payload),
            make_completion_response(reply_only_payload),
            make_completion_response(reply_only_payload),
        ],
    ):
        assert client.post("/turn", json={"session_id": session_id, "text": "Tell me Pro price"}).status_code == 200
        resp2 = client.post("/turn", json={"session_id": session_id, "text": "Okay thanks"}).json()

    assert "brief connection issue" in resp2["reply"]
    lead = db.get_lead(lead_id)
    assert lead["score"] == 82
    assert lead["sentiment"] == "positive"
    assert lead["name"] == "Dr. Mehta"


# Verify non-finite (1e999 / inf) and invalid (bool, string, out-of-range) scores never cause HTTP 500.
def test_invalid_and_non_finite_scores_never_500(client, monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)

    start_data = client.post("/start_call", json={"phone": "+919800000017", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    db.merge_lead_fields(lead_id, score=75, sentiment="positive")

    for bad_score in (float("inf"), True, "ninety", -5, 150):
        bad_turn = {
            "reply": "Some reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": bad_score,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
        }
        with patch("app.brain.generate_turn_response", return_value=bad_turn):
            r = client.post("/turn", json={"session_id": session_id, "text": "Checking"})
        assert r.status_code == 200

    # Also test 1e999 raw JSON returned to /end_call summary parser.
    raw_msg = MagicMock()
    raw_msg.content = '{"summary":"Good call","overall_sentiment":"positive","final_score":1e999}'
    raw_choice = MagicMock()
    raw_choice.message = raw_msg
    raw_resp = MagicMock()
    raw_resp.choices = [raw_choice]

    with patch.object(summary.LLM_CLIENT.chat.completions, "create", return_value=raw_resp):
        end_resp = client.post("/end_call", json={"session_id": session_id})

    assert end_resp.status_code == 200
    assert "auto-generated (model unavailable)" in end_resp.json()["summary"]
    assert end_resp.json()["final_score"] == 75
    assert db.get_lead(lead_id)["score"] == 75


# Verify secondary provider rejects an overdue response when wall-clock deadline has elapsed.
def test_secondary_deadline_enforced_after_slow_call(monkeypatch):
    payload = {
        "reply": "Too late",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 60,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": None,
    }
    sec_client, sec_model = make_secondary_mock([make_completion_response(payload)])
    monkeypatch.setattr(brain, "get_secondary_provider", lambda: (sec_client, sec_model))

    times = iter([100.0, 112.0])
    monkeypatch.setattr(brain.time, "monotonic", lambda: next(times, 112.0))

    result = brain.call_secondary_for_turn(
        chat_messages=[{"role": "user", "content": "Hi"}],
        language="en",
        deadline=110.0,
    )
    assert result is None


# Verify negative refusal ("No, please don't book that") cannot book and clears pending_slot.
def test_negative_booking_confirmation_clears_pending_slot(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000018", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        assert client.post("/turn", json={"session_id": session_id, "text": "Any slot?"}).status_code == 200
    assert db.get_session(session_id)["pending_slot"] == target_slot

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    with patch("app.brain.generate_turn_response", return_value=false_book):
        resp = client.post("/turn", json={"session_id": session_id, "text": "No, please don't book that"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "enquiry"
    assert target_slot in client.get("/api/slots").json()["slots"]
    assert db.get_session(session_id)["pending_slot"] is None


# Verify ambiguous text ("Maybe tomorrow?" or "Yesterday I checked") cannot book even with book_appointment intent.
def test_ambiguous_confirmation_cannot_book(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000019", "language": "en"}).json()["session_id"]

    propose = {
        "reply": f"Shall I book {target_slot}?",
        "language": "en",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Any slot?"})

    false_book = dict(propose, intent="book_appointment", reply="Booked!")
    for ambiguous_text in ("Maybe tomorrow?", "Yesterday I checked my diary"):
        with patch("app.brain.generate_turn_response", return_value=false_book):
            resp = client.post("/turn", json={"session_id": session_id, "text": ambiguous_text})
        assert resp.status_code == 200
        assert resp.json()["intent"] == "enquiry"
        assert target_slot in client.get("/api/slots").json()["slots"]

    # Now explicit affirmative confirmation still books cleanly.
    with patch("app.brain.generate_turn_response", return_value=false_book):
        confirm_resp = client.post("/turn", json={"session_id": session_id, "text": "Haan ji, book kar do"})
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["intent"] == "book_appointment"
    assert target_slot not in client.get("/api/slots").json()["slots"]


# Verify a turn cannot commit after /end_call ends the session mid-flight (returns 409).
def test_turn_cannot_commit_after_session_ended_mid_flight(client):
    session_id = client.post("/start_call", json={"phone": "+919800000020", "language": "en"}).json()["session_id"]
    before_count = len(db.get_all_messages(session_id))

    def end_session_during_llm(*args, **kwargs):
        with patch("app.summary.call_analyst_llm", return_value=None):
            summary.generate_and_save_summary(session_id, db.get_session(session_id)["lead_id"])
        return {
            "reply": "Late reply",
            "language": "en",
            "intent": "enquiry",
            "sentiment": "positive",
            "lead_score": 80,
            "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
            "slot_datetime": None,
            "fallback": False,
        }

    with patch("app.brain.generate_turn_response", side_effect=end_session_during_llm):
        resp = client.post("/turn", json={"session_id": session_id, "text": "Hello?"})

    assert resp.status_code == 409
    assert len(db.get_all_messages(session_id)) == before_count


# Verify summary refreshes if a concurrent turn commits while the summary LLM is running.
def test_summary_refreshes_if_concurrent_turn_committed(client):
    start_data = client.post("/start_call", json={"phone": "+919800000021", "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    calls_seen = []

    def concurrent_analyst(transcript):
        calls_seen.append(transcript)
        if len(calls_seen) == 1:
            db.save_message(session_id, lead_id, "user", "Actually book Pro plan!", "en", "positive", 90)
            return {
                "summary": "Stale summary",
                "objections": [],
                "buying_signals": [],
                "next_best_action": "None",
                "overall_sentiment": "neutral",
                "final_score": 40,
                "sentiment_timeline": ["neutral"],
            }
        return {
            "summary": "Updated summary with Pro plan booking.",
            "objections": [],
            "buying_signals": ["Book Pro plan"],
            "next_best_action": "Send invoice",
            "overall_sentiment": "positive",
            "final_score": 90,
            "sentiment_timeline": ["positive"],
        }

    with patch("app.summary.call_analyst_llm", side_effect=concurrent_analyst):
        resp = client.post("/end_call", json={"session_id": session_id})

    assert resp.status_code == 200
    assert len(calls_seen) == 2
    assert "Actually book Pro plan!" in calls_seen[1]
    assert resp.json()["final_score"] == 90


# Verify unsupported API language 'pa' is rejected with 422 on /start_call and /turn.
def test_unsupported_api_language_rejected(client):
    bad_start = client.post("/start_call", json={"phone": "+919800000022", "language": "pa"})
    assert bad_start.status_code == 422

    session_id = client.post("/start_call", json={"phone": "+919800000022", "language": "en"}).json()["session_id"]
    bad_turn = client.post("/turn", json={"session_id": session_id, "text": "Hello", "language": "pa"})
    assert bad_turn.status_code == 422


# Verify scripted booking confirmation returns the session's reply language even if LLM tagged another language.
def test_booking_confirmation_returns_matching_reply_language(client):
    slots = client.get("/api/slots").json()["slots"]
    target_slot = slots[0]
    session_id = client.post("/start_call", json={"phone": "+919800000023", "language": "hi"}).json()["session_id"]

    propose = {
        "reply": f"Kya main {target_slot} book karoon?",
        "language": "hinglish",
        "intent": "enquiry",
        "sentiment": "positive",
        "lead_score": 70,
        "extracted": {"name": None, "email": None, "budget": None, "need": None, "timeline": None},
        "slot_datetime": target_slot,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=propose):
        client.post("/turn", json={"session_id": session_id, "text": "Demo kab hai?"})

    confirm_mismatched_lang = dict(propose, intent="book_appointment", language="hinglish")
    with patch("app.brain.generate_turn_response", return_value=confirm_mismatched_lang):
        resp = client.post("/turn", json={"session_id": session_id, "text": "हाँ, बुक कर दो"})

    assert resp.status_code == 200
    assert resp.json()["intent"] == "book_appointment"
    assert resp.json()["language"] == "hi"
    assert "आपका डेमो" in resp.json()["reply"]


# Verify GET / and both static assets return HTTP 200.
def test_frontend_routes_return_200(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/call.css").status_code == 200
    assert client.get("/static/call.js").status_code == 200


# Verify empty database dashboard renders HTTP 200 with zeroed KPIs and empty-state messages.
def test_empty_dashboard_renders_cleanly(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert client.get("/static/dashboard.css").status_code == 200
    html = resp.text
    assert "0.0%" in html
    assert "0 booked / 0 total leads" in html
    assert "No leads recorded yet." in html
    assert "No appointments booked yet." in html
    assert "No completed calls yet." in html


# Verify seeded dashboard KPIs, masked phones, and call detail link match seeded records.
def test_dashboard_kpis_and_masked_phones_with_seed_data(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Seed has 3 leads (scores 88, 72, 48 -> avg 69.3), 3 calls, 3 qualified, 1 booked (33.3%)
    assert "69.3" in html
    assert "33.3%" in html
    assert "1 booked / 3 total leads" in html
    assert "Dr. Ananya Sharma" in html
    assert "Dr. Harpreet Gill" in html
    assert "/dashboard/calls/1" in html

    # Raw phone numbers must never appear in HTML; masked numbers must appear instead.
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "+919811223344" not in html
    assert "+919822334455" not in html
    assert "+91******3210" in html


# Verify existing call details page returns 200 with transcript, timeline, masked phone, and back link.
def test_call_detail_page_renders_seeded_call(client):
    from app.seed import seed_database

    seed_database()
    resp = client.get("/dashboard/calls/1")
    assert resp.status_code == 200
    html = resp.text

    assert "Dr. Ananya Sharma" in html
    assert "+91******3210" in html
    assert "+919876543210" not in html
    assert "9876543210" not in html
    assert "Wanted to confirm patient data stays in India" in html
    assert "Booked an onboarding demo slot" in html
    assert "Back to Dashboard" in html
    assert 'href="/dashboard"' in html
    assert "Step 1" in html


# Verify nonexistent call ID on /dashboard/calls/{call_id} returns HTTP 404.
def test_call_detail_nonexistent_returns_404(client):
    resp = client.get("/dashboard/calls/9999")
    assert resp.status_code == 404


# Verify customer-supplied and LLM-generated HTML/script tags are escaped on both dashboard pages.
def test_dashboard_escapes_customer_and_llm_html(client):
    xss_name = "<script>alert('name_xss')</script>"
    xss_need = "<img src=x onerror=alert('need_xss')>"
    xss_summary = "<script>alert('summary_xss')</script>"
    xss_msg = "<svg onload=alert('msg_xss')>"
    raw_phone = "+919877665544"

    start_data = client.post("/start_call", json={"phone": raw_phone, "language": "en"}).json()
    session_id = start_data["session_id"]
    lead_id = start_data["lead_id"]

    turn_payload = {
        "reply": f"Noted {xss_msg}",
        "language": "en",
        "intent": "update_crm",
        "sentiment": "positive",
        "lead_score": 65,
        "extracted": {"name": xss_name, "email": None, "budget": None, "need": xss_need, "timeline": None},
        "slot_datetime": None,
        "fallback": False,
    }
    with patch("app.brain.generate_turn_response", return_value=turn_payload):
        assert client.post("/turn", json={"session_id": session_id, "text": xss_msg}).status_code == 200

    summary_payload = {
        "summary": xss_summary,
        "objections": ["<b>No</b>"],
        "buying_signals": ["<i>Yes</i>"],
        "next_best_action": "<u>Call back</u>",
        "overall_sentiment": "positive",
        "final_score": 65,
        "sentiment_timeline": ["positive"],
    }
    with patch("app.summary.call_analyst_llm", return_value=summary_payload):
        assert client.post("/end_call", json={"session_id": session_id}).status_code == 200

    dash_html = client.get("/dashboard").text
    assert raw_phone not in dash_html
    assert "+91******5544" in dash_html
    assert xss_name not in dash_html
    assert "&lt;script&gt;alert(&#39;name_xss&#39;)&lt;/script&gt;" in dash_html
    assert xss_need not in dash_html
    assert "&lt;img src=x onerror=alert(&#39;need_xss&#39;)&gt;" in dash_html

    detail_html = client.get("/dashboard/calls/1").text
    assert raw_phone not in detail_html
    assert "+91******5544" in detail_html
    assert xss_summary not in detail_html
    assert "&lt;script&gt;alert(&#39;summary_xss&#39;)&lt;/script&gt;" in detail_html
    assert xss_msg not in detail_html
    assert "&lt;svg onload=alert(&#39;msg_xss&#39;)&gt;" in detail_html
