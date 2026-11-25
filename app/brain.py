import json
import math
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import BadRequestError, OpenAI, RateLimitError

from app.db import LANGS

load_dotenv()

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
VALID_INTENTS = {"enquiry", "book_appointment", "update_crm", "end_call"}
VALID_SENTIMENTS = {"positive", "neutral", "negative"}
VALID_LANGUAGES = {"en", "hi", "hinglish"}
EXTRACTED_KEYS = ("name", "email", "budget", "need", "timeline")
TURN_DEADLINE_SECONDS = 10.0
DEFAULT_RETRY_AFTER_SECONDS = 1.5
MAX_RETRY_AFTER_SECONDS = 3.0
SECONDARY_RETRY_STATUSES = {500, 502, 503, 504}
SECONDARY_RETRY_DELAY_SECONDS = 1.0
SECONDARY_MAX_ATTEMPTS = 3

# Create the OpenAI-compatible client once at startup with no automatic SDK retries and 8s timeout.
LLM_CLIENT = OpenAI(
    api_key=os.getenv("LLM_API_KEY") or "not-set",
    base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
    max_retries=0,
    timeout=8.0,
)
_SECONDARY_CLIENT_CACHE: dict[str, object] = {"key": None, "base_url": None, "client": None}


# Read the configured model name from the environment so it can be swapped in .env only.
def get_llm_model() -> str:
    return os.getenv("LLM_MODEL", "openai/gpt-4o-mini")


# Return the optional secondary (client, model) pair when all three fallback env vars are configured.
def get_secondary_provider() -> tuple[OpenAI, str] | None:
    api_key = os.getenv("LLM_FALLBACK_API_KEY", "").strip()
    base_url = os.getenv("LLM_FALLBACK_BASE_URL", "").strip()
    model = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    if not (api_key and base_url and model):
        return None

    if (
        _SECONDARY_CLIENT_CACHE["client"] is None
        or _SECONDARY_CLIENT_CACHE["key"] != api_key
        or _SECONDARY_CLIENT_CACHE["base_url"] != base_url
    ):
        _SECONDARY_CLIENT_CACHE["key"] = api_key
        _SECONDARY_CLIENT_CACHE["base_url"] = base_url
        _SECONDARY_CLIENT_CACHE["client"] = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=8.0,
        )
    return _SECONDARY_CLIENT_CACHE["client"], model


# Read and concatenate all markdown files from /kb once at startup so turns avoid disk I/O.
def load_knowledge_base() -> str:
    if not KB_DIR.exists():
        return ""
    sections: list[str] = []
    for path in sorted(KB_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"--- {path.name} ---\n{content}")
    return "\n\n".join(sections)


KB_TEXT = load_knowledge_base()


# Build the system prompt with persona, cached KB, non-PII lead context, free slots, and JSON schema.
def build_system_prompt(lead: dict, available_slots: list[str], language: str) -> str:
    slots_preview = ", ".join(available_slots) if available_slots else "No slots currently available"
    lang_schema = "en|hi|hinglish"
    lead_summary = json.dumps(
        {
            "name": lead.get("name"),
            "need": lead.get("need"),
            "budget": lead.get("budget"),
            "timeline": lead.get("timeline"),
        },
        ensure_ascii=False,
    )

    return (
        "You are Zenvo's friendly, concise AI sales voice agent speaking on a live phone call.\n"
        "Zenvo is a clinic appointment-reminder SaaS.\n\n"
        "STRICT RULES:\n"
        "1. Answer ONLY from the Knowledge Base below. If the answer is unknown or not in the Knowledge Base, "
        "say you will have a human follow up.\n"
        f'2. Reply in the SAME language as the user (English, Hindi, or Hinglish). If the user writes in Punjabi or another unsupported language, reply in Hinglish and set language to "hinglish". Preferred call language: {language}.\n'
        "3. Keep your reply to a MAXIMUM of 2 short, natural phone-style sentences. No markdown, no bullet points, no emojis.\n"
        "4. Return ONLY valid JSON with keys in this EXACT order:\n"
        f'{{"reply": str, "language": "{lang_schema}", "intent": "enquiry|book_appointment|update_crm|end_call", '
        '"sentiment": "positive|neutral|negative", "lead_score": 0-100, '
        '"extracted": {"name":null,"email":null,"budget":null,"need":null,"timeline":null}, '
        '"slot_datetime": null}\n\n'
        "INTENT GUIDE:\n"
        "- enquiry: general question about Zenvo pricing, features, security, onboarding, or policies.\n"
        "- update_crm: user shares personal or clinic details (name, email, budget, need, timeline).\n"
        "- book_appointment: When you propose or discuss a specific slot, set slot_datetime to it. "
        "Use intent book_appointment ONLY when the user has clearly said yes to the slot you proposed "
        "in your previous message. (Put exact 'YYYY-MM-DD HH:MM' in slot_datetime.)\n"
        "- end_call: user says goodbye, thanks and hangs up, or asks to end the call.\n\n"
        f"SAVED LEAD INFO:\n{lead_summary}\n\n"
        f"AVAILABLE DEMO SLOTS (YYYY-MM-DD HH:MM):\n{slots_preview}\n\n"
        f"KNOWLEDGE BASE:\n{KB_TEXT}"
    )


# Convert recent DB message rows into OpenAI chat message dictionaries plus the new user turn.
def build_chat_messages(system_prompt: str, recent_messages: list[dict], user_text: str) -> list[dict]:
    messages = [{"role": "system", "content": system_prompt}]
    for msg in recent_messages:
        role = "assistant" if msg.get("role") == "agent" else "user"
        messages.append({"role": role, "content": msg.get("text", "")})
    messages.append({"role": "user", "content": user_text})
    return messages


# Strip markdown code fences and parse JSON safely, returning (parsed_dict, error_type).
def safe_parse_json(raw_text: str) -> tuple[dict | None, str | None]:
    if not raw_text or not raw_text.strip():
        return None, "EmptyResponse"
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed, None
        return None, "NotJSONObject"
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None, type(exc).__name__
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed, None
            return None, "NotJSONObject"
        except json.JSONDecodeError as inner_exc:
            return None, type(inner_exc).__name__


# Validate a numeric score as a finite number within 0..100, rejecting bools, strings, NaN, and Inf.
def parse_valid_score(raw_score: object) -> int | None:
    if raw_score is None or isinstance(raw_score, bool):
        return None
    if not isinstance(raw_score, (int, float)):
        return None
    if not math.isfinite(raw_score):
        return None
    if raw_score < 0 or raw_score > 100:
        return None
    return int(raw_score)


# Normalize and strictly validate parsed LLM output; return None if reply, sentiment, or score is invalid.
def normalize_llm_result(parsed: dict, default_language: str) -> dict | None:
    fallback_lang = default_language if default_language in VALID_LANGUAGES else "en"
    raw_lang = str(parsed.get("language") or fallback_lang).lower().strip()
    language = raw_lang if raw_lang in VALID_LANGUAGES else fallback_lang

    raw_intent = str(parsed.get("intent", "enquiry")).lower().strip()
    intent = raw_intent if raw_intent in VALID_INTENTS else "enquiry"

    raw_sentiment = parsed.get("sentiment")
    if not isinstance(raw_sentiment, str):
        return None
    sentiment = raw_sentiment.lower().strip()
    if sentiment not in VALID_SENTIMENTS:
        return None

    lead_score = parse_valid_score(parsed.get("lead_score"))
    if lead_score is None:
        return None

    raw_extracted = parsed.get("extracted")
    if not isinstance(raw_extracted, dict):
        raw_extracted = {}
    extracted = {key: raw_extracted.get(key) for key in EXTRACTED_KEYS}

    reply = str(parsed.get("reply") or "").strip()
    if not reply:
        return None

    slot_datetime = parsed.get("slot_datetime")
    if slot_datetime is not None:
        slot_datetime = str(slot_datetime).strip() or None

    return {
        "reply": reply,
        "language": language,
        "intent": intent,
        "sentiment": sentiment,
        "lead_score": lead_score,
        "extracted": extracted,
        "slot_datetime": slot_datetime,
        "fallback": False,
    }


# Provide a polite fallback payload marked fallback=True so CRM fields are not overwritten.
def fallback_reply(language: str) -> dict:
    lang = language if language in VALID_LANGUAGES else "en"
    replies = {
        "en": "Sorry, I had a brief connection issue. Let me have a human specialist follow up with you shortly.",
        "hi": "Maaf kijiye, thodi network samasya aa gayi hai. Hamare team member jaldi aapse sampark karenge.",
        "hinglish": "Sorry, thoda network issue aa gaya hai. Hamare specialist aapko jaldi follow up karenge.",
    }

    return {
        "reply": replies[lang],
        "language": lang,
        "intent": "enquiry",
        "sentiment": "neutral",
        "lead_score": 20,
        "extracted": {key: None for key in EXTRACTED_KEYS},
        "slot_datetime": None,
        "fallback": True,
    }


# Parse Retry-After from a 429 response, capping at 3.0s and defaulting to 1.5s if missing.
def get_retry_after_seconds(exc: RateLimitError) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw_header = headers.get("retry-after") if headers else None
    if raw_header is not None:
        try:
            parsed = float(raw_header)
            if parsed >= 0:
                return min(parsed, MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            pass
    return DEFAULT_RETRY_AFTER_SECONDS


# Execute one completion call with json_object fallback while respecting the caller's deadline.
def create_completion_with_json_fallback(
    client: OpenAI,
    model: str,
    messages: list[dict],
    temperature: float,
    max_timeout: float,
    deadline: float | None = None,
) -> str:
    remaining = max_timeout if deadline is None else min(max_timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Turn deadline exceeded")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=temperature,
            timeout=remaining,
        )
    except BadRequestError:
        remaining_after_err = max_timeout if deadline is None else min(max_timeout, deadline - time.monotonic())
        if remaining_after_err <= 0:
            raise TimeoutError("Turn deadline exceeded before JSON-mode fallback")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout=remaining_after_err,
        )
    return response.choices[0].message.content or ""


# Shared helper for brain and summary: call LLM with one RateLimitError (429) retry within budget.
def call_llm_with_429_retry(
    client: OpenAI,
    model: str,
    messages: list[dict],
    temperature: float,
    max_timeout: float,
    deadline: float | None,
    log_prefix: str,
) -> tuple[str, float]:
    start = time.monotonic()
    try:
        for retry_attempt in range(2):
            try:
                content = create_completion_with_json_fallback(
                    client=client,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_timeout=max_timeout,
                    deadline=deadline,
                )
                return content, (time.monotonic() - start) * 1000
            except RateLimitError as exc:
                print(
                    f"{log_prefix} LLM call error on attempt {retry_attempt + 1}: "
                    f"error_type={type(exc).__name__}"
                )
                if retry_attempt == 1:
                    raise
                wait_seconds = get_retry_after_seconds(exc)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.2:
                        raise TimeoutError("Turn deadline exceeded before 429 retry") from exc
                    wait_seconds = min(wait_seconds, max(0.0, remaining - 0.2))
                time.sleep(wait_seconds)
    finally:
        latency_ms = (time.monotonic() - start) * 1000
        print(f"{log_prefix} LLM call latency: {latency_ms:.1f} ms")


# Execute one per-turn LLM completion call within the remaining turn deadline.
def call_llm_once(model: str, messages: list[dict], deadline: float) -> tuple[str, float]:
    return call_llm_with_429_retry(
        client=LLM_CLIENT,
        model=model,
        messages=messages,
        temperature=0.3,
        max_timeout=8.0,
        deadline=deadline,
        log_prefix="[brain]",
    )


# Extract an HTTP status code from an OpenAI SDK exception if available.
def get_exception_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return int(status_code) if isinstance(status_code, int) else None


# Shared secondary-provider caller: retries only HTTP 500/502/503/504 up to 2 times within budget.
def call_secondary_with_5xx_retry(
    chat_messages: list[dict],
    temperature: float,
    max_timeout: float,
    deadline: float,
    log_prefix: str,
) -> dict | None:
    secondary = get_secondary_provider()
    if secondary is None:
        return None
    sec_client, sec_model = secondary

    for attempt in range(1, SECONDARY_MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            print(f"{log_prefix} Secondary attempt {attempt} skipped: insufficient remaining time budget")
            return None

        try:
            response = sec_client.chat.completions.create(
                model=sec_model,
                messages=chat_messages,
                response_format={"type": "json_object"},
                extra_body={"reasoning_effort": "low"},
                temperature=temperature,
                timeout=min(max_timeout, remaining),
            )
            raw_output = response.choices[0].message.content or ""
            if time.monotonic() > deadline:
                print(f"{log_prefix} Secondary attempt {attempt} exceeded wall-clock deadline")
                return None
            parsed, err_type = safe_parse_json(raw_output)
            if parsed is not None:
                return parsed
            print(
                f"{log_prefix} Secondary attempt {attempt} failed: "
                f"error_type={err_type}, status_code=200"
            )
            return None
        except Exception as exc:
            status_code = get_exception_status_code(exc)
            print(
                f"{log_prefix} Secondary attempt {attempt} failed: "
                f"error_type={type(exc).__name__}, status_code={status_code}"
            )
            if status_code not in SECONDARY_RETRY_STATUSES or attempt == SECONDARY_MAX_ATTEMPTS:
                return None
            if deadline - time.monotonic() <= SECONDARY_RETRY_DELAY_SECONDS + 0.2:
                print(f"{log_prefix} Secondary retry skipped: insufficient remaining time budget")
                return None
            time.sleep(SECONDARY_RETRY_DELAY_SECONDS)

    return None


# Try the optional secondary provider with bounded 5xx retries for a conversation turn.
def call_secondary_for_turn(chat_messages: list[dict], language: str, deadline: float) -> dict | None:
    parsed = call_secondary_with_5xx_retry(
        chat_messages=chat_messages,
        temperature=0.3,
        max_timeout=8.0,
        deadline=deadline,
        log_prefix="[brain]",
    )
    if parsed is None:
        return None
    return normalize_llm_result(parsed, language)


# Run the per-turn LLM call with 429 retry, parse retry, optional secondary provider, and fallback.
def generate_turn_response(
    lead: dict,
    recent_messages: list[dict],
    available_slots: list[str],
    user_text: str,
    language: str,
) -> dict:
    system_prompt = build_system_prompt(lead, available_slots, language)
    chat_messages = build_chat_messages(system_prompt, recent_messages, user_text)
    model = get_llm_model()
    deadline = time.monotonic() + TURN_DEADLINE_SECONDS

    for attempt in range(2):
        if time.monotonic() >= deadline:
            print(f"[brain] Turn deadline exceeded before attempt {attempt + 1}.")
            break
        try:
            raw_output, latency_ms = call_llm_once(model, chat_messages, deadline)
            parsed, err_type = safe_parse_json(raw_output)
            if parsed is not None:
                normalized = normalize_llm_result(parsed, language)
                if normalized is not None and not normalized.get("fallback"):
                    print("[brain] provider=primary")
                    return normalized
                err_type = "InvalidSchemaFields"
            print(
                f"[brain] JSON parse failed on attempt {attempt + 1}: "
                f"error_type={err_type}, length={len(raw_output)}, latency_ms={latency_ms:.1f}"
            )
        except Exception as exc:
            print(f"[brain] LLM call error on attempt {attempt + 1}: error_type={type(exc).__name__}")
            break

    secondary_result = call_secondary_for_turn(chat_messages, language, deadline)
    if secondary_result is not None and not secondary_result.get("fallback"):
        print("[brain] provider=fallback")
        return secondary_result

    return fallback_reply(language)
