import time

from app import db
from app.brain import (
    LLM_CLIENT,
    VALID_SENTIMENTS,
    call_llm_with_429_retry,
    call_secondary_with_5xx_retry,
    get_llm_model,
    parse_valid_score,
    safe_parse_json,
)


# Format all session messages into a readable speaker-labeled transcript for the sales analyst LLM.
def build_transcript(messages: list[dict]) -> str:
    if not messages:
        return "No messages recorded in this call."
    lines: list[str] = []
    for msg in messages:
        speaker = "AGENT" if msg.get("role") == "agent" else "USER"
        lines.append(f"{speaker}: {msg.get('text', '')}")
    return "\n".join(lines)


# Build a fallback summary using only the lead's stored trusted score and sentiment from leads.
def build_fallback_summary(messages: list[dict], lead: dict | None) -> dict:
    user_turns = [m for m in messages if m.get("role") == "user"]
    timeline = [m.get("sentiment", "neutral") for m in user_turns] or ["neutral"]

    trusted_score = int(lead["score"]) if lead and lead.get("score") is not None else 0
    raw_sentiment = lead.get("sentiment", "neutral") if lead else "neutral"
    trusted_sentiment = raw_sentiment if raw_sentiment in VALID_SENTIMENTS else "neutral"

    lead_name = lead.get("name") if lead and lead.get("name") else "Caller"
    lead_need = lead.get("need") if lead and lead.get("need") else "general enquiry"
    lead_status = lead.get("status") if lead and lead.get("status") else "new"

    return {
        "summary": (
            f"auto-generated (model unavailable): {lead_name} discussed {lead_need} "
            f"(status: {lead_status}, turns: {len(user_turns)})."
        ),
        "objections": [],
        "buying_signals": [],
        "next_best_action": "Have a sales representative review the call and follow up.",
        "overall_sentiment": trusted_sentiment,
        "final_score": max(0, min(100, trusted_score)),
        "sentiment_timeline": timeline,
    }


# Check that a parsed analyst summary contains valid summary text, sentiment, and finite 0-100 score.
def is_valid_summary_payload(parsed: dict | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    if not str(parsed.get("summary") or "").strip():
        return False
    raw_sentiment = parsed.get("overall_sentiment")
    if not isinstance(raw_sentiment, str) or raw_sentiment.lower().strip() not in VALID_SENTIMENTS:
        return False
    return parse_valid_score(parsed.get("final_score")) is not None


# Validate and normalize the analyst JSON payload so DB writes and API responses stay consistent.
def normalize_summary_result(parsed: dict, messages: list[dict], lead: dict | None) -> dict:
    fallback = build_fallback_summary(messages, lead)

    summary_text = str(parsed.get("summary") or fallback["summary"]).strip()
    objections = parsed.get("objections") if isinstance(parsed.get("objections"), list) else []
    buying_signals = parsed.get("buying_signals") if isinstance(parsed.get("buying_signals"), list) else []
    next_best_action = str(parsed.get("next_best_action") or fallback["next_best_action"]).strip()

    raw_sentiment = parsed.get("overall_sentiment")
    sentiment_clean = raw_sentiment.lower().strip() if isinstance(raw_sentiment, str) else ""
    overall_sentiment = sentiment_clean if sentiment_clean in VALID_SENTIMENTS else fallback["overall_sentiment"]

    parsed_score = parse_valid_score(parsed.get("final_score"))
    final_score = parsed_score if parsed_score is not None else fallback["final_score"]

    timeline = parsed.get("sentiment_timeline")
    if not isinstance(timeline, list):
        timeline = fallback["sentiment_timeline"]

    return {
        "summary": summary_text,
        "objections": [str(item) for item in objections],
        "buying_signals": [str(item) for item in buying_signals],
        "next_best_action": next_best_action,
        "overall_sentiment": overall_sentiment,
        "final_score": final_score,
        "sentiment_timeline": [str(item) for item in timeline],
    }


# Try the optional secondary provider with bounded 5xx retries for end-of-call summary.
def call_secondary_for_summary(chat_messages: list[dict], deadline: float) -> dict | None:
    parsed = call_secondary_with_5xx_retry(
        chat_messages=chat_messages,
        temperature=0.2,
        max_timeout=20.0,
        deadline=deadline,
        log_prefix="[summary]",
    )
    if not is_valid_summary_payload(parsed):
        return None
    return parsed


# Call the shared LLM helper with 429 retry and optional secondary provider within a 20s timeout.
def call_analyst_llm(transcript: str) -> dict | None:
    system_prompt = (
        "You are a senior sales analyst reviewing a Zenvo clinic SaaS sales call transcript.\n"
        "Return ONLY valid JSON with these exact keys:\n"
        '{"summary": str, "objections": [str], "buying_signals": [str], '
        '"next_best_action": str, "overall_sentiment": "positive|neutral|negative", '
        '"final_score": 0-100, "sentiment_timeline": [str]}'
    )
    chat_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"CALL TRANSCRIPT:\n{transcript}"},
    ]

    model = get_llm_model()
    deadline = time.monotonic() + 20.0
    try:
        raw_text, latency_ms = call_llm_with_429_retry(
            client=LLM_CLIENT,
            model=model,
            messages=chat_messages,
            temperature=0.2,
            max_timeout=20.0,
            deadline=deadline,
            log_prefix="[summary]",
        )
        parsed, err_type = safe_parse_json(raw_text)
        if parsed is not None and is_valid_summary_payload(parsed):
            return parsed
        if parsed is not None:
            err_type = "InvalidSummaryFields"
        print(
            f"[summary] JSON parse failed on attempt 1: "
            f"error_type={err_type}, length={len(raw_text)}, latency_ms={latency_ms:.1f}"
        )
    except Exception as exc:
        print(f"[summary] LLM summary error on attempt 1: error_type={type(exc).__name__}")

    return call_secondary_for_summary(chat_messages, deadline)


# Return the existing summary for an ended session, or summarize, save, and end the session once.
def generate_and_save_summary(session_id: str, lead_id: int) -> dict:
    existing = db.get_call_summary(session_id)
    if existing is not None:
        return existing

    messages = db.get_all_messages(session_id)
    lead = db.get_lead(lead_id)
    transcript = build_transcript(messages)

    parsed = call_analyst_llm(transcript)
    latest_messages = db.get_all_messages(session_id)
    if len(latest_messages) != len(messages):
        messages = latest_messages
        lead = db.get_lead(lead_id)
        parsed = call_analyst_llm(build_transcript(messages))

    if is_valid_summary_payload(parsed):
        summary_data = normalize_summary_result(parsed, messages, lead)
        is_fallback = False
    else:
        summary_data = build_fallback_summary(messages, lead)
        is_fallback = True

    return db.finalize_call(session_id, lead_id, summary_data, is_fallback=is_fallback)
