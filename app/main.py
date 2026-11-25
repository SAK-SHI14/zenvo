import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from app import brain, dashboard, db, handlers, router, summary
from app.db import LANGS

load_dotenv()

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_REGEX = re.compile(r"^[0-9+]{1,15}$")
FIELD_MAX_LENGTHS = {
    "name": 60,
    "need": 200,
    "timeline": 200,
    "budget": 50,
}
MAX_TURNS_PER_SESSION = 60


# Initialize the SQLite schema when the FastAPI application starts up.
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Zenvo AI Sales Voice Agent", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartCallRequest(BaseModel):
    phone: str | None = None
    language: LANGS = LANGS.en

    # Limit phone input to digits and '+' up to 15 characters, treating empty string as None.
    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if cleaned == "":
            return None
        if not PHONE_REGEX.match(cleaned):
            raise ValueError("phone must contain only digits and '+' up to 15 characters")
        return cleaned


class TurnRequest(BaseModel):
    session_id: str
    text: str
    language: LANGS | None = None

    # Strip whitespace and require caller text to be between 1 and 500 characters.
    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not (1 <= len(cleaned) <= 500):
            raise ValueError("text must be between 1 and 500 characters")
        return cleaned


class EndCallRequest(BaseModel):
    session_id: str


# Build a friendly opening greeting in the caller's chosen language, personalizing if returning.
def build_greeting(language: str, returning_lead: dict | None) -> str:
    lang = (language or LANGS.en.value).lower().strip()
    name = returning_lead.get("name") if returning_lead else None
    need = returning_lead.get("need") if returning_lead else None

    if lang == LANGS.hi.value:
        if name:
            extra = f" Pichhli baar aapne {need} ke baare mein pucha tha." if need else ""
            return f"Namaste {name} ji, Zenvo mein aapka swagat hai!{extra} Aaj main aapki kya madad kar sakti hoon?"
        return "Namaste, Zenvo mein aapka swagat hai! Main aapke clinic ke appointment reminders ke baare mein kaise madad kar sakti hoon?"

    if lang == LANGS.hinglish.value:
        if name:
            extra = f" Last time aapne {need} ke liye enquiry ki thi." if need else ""
            return f"Hello {name}, Zenvo mein welcome hai!{extra} Aaj main aapki kya help kar sakti hoon?"
        return "Hello! Zenvo mein welcome hai. Main aapke clinic ke appointment reminders mein aaj kaise help kar sakti hoon?"

    if name:
        extra = f" Last time we spoke about {need}." if need else ""
        return f"Hi {name}, welcome back to Zenvo!{extra} How can I help your clinic today?"
    return "Hi, welcome to Zenvo! I can help you reduce clinic no-shows with automated reminders. How can I help today?"


AFFIRM_PHRASES = (
    "go ahead",
    "please book",
    "book it",
    "book that",
    "sounds good",
    "works for me",
    "that works",
    "lock it",
    "do it",
    "haan ji",
    "han ji",
    "theek hai",
    "thik hai",
    "kar do",
    "kardo",
    "book kar do",
    "book kardo",
    "confirm kar do",
    "confirm karo",
    "kar dijiye",
    "ठीक है",
    "कर दो",
    "कर दीजिए",
    "बुक कर दो",
    "बुक कर दीजिए",
    "कन्फर्म करो",
    "कन्फर्म कर दो",
    "हाँ जी",
)
AFFIRM_TOKENS = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "agreed",
    "definitely",
    "absolutely",
    "haan",
    "han",
    "hanji",
    "haanji",
    "theek",
    "thik",
    "pakka",
    "bilkul",
    "हाँ",
    "हां",
    "बिल्कुल",
    "पक्का",
}
REFUSE_PHRASES = (
    "no thanks",
    "no thank you",
    "not now",
    "not that",
    "don't book",
    "dont book",
    "do not book",
    "never mind",
    "change slot",
    "different slot",
    "another slot",
    "other time",
    "mat karo",
    "mat kar",
    "rehne do",
    "nahi chahiye",
    "book nahi",
    "मत करो",
    "रद्द करो",
    "नहीं चाहिए",
)
REFUSE_TOKENS = {
    "no",
    "nope",
    "nah",
    "nahi",
    "nahin",
    "naa",
    "mat",
    "cancel",
    "never",
    "don't",
    "dont",
    "नहीं",
    "मत",
}
AMBIGUOUS_TOKENS = {
    "maybe",
    "perhaps",
    "might",
    "unsure",
    "check",
    "think",
    "later",
    "probably",
    "confused",
    "shayad",
    "soch",
    "dekhte",
    "pata",
    "शायद",
}


# Check whether normalized word tokens contain a given multi-word phrase cleanly.
def contains_phrase(tokens: list[str], phrase: str) -> bool:
    parts = phrase.split()
    if len(tokens) < len(parts):
        return False
    for i in range(len(tokens) - len(parts) + 1):
        if tokens[i : i + len(parts)] == parts:
            return True
    return False


# Classify the caller's latest message conservatively as confirm, reject, or ambiguous.
def classify_booking_confirmation(user_text: str) -> str:
    cleaned = re.sub(r"[^\w\s\u0900-\u097F']", " ", user_text.lower())
    tokens = cleaned.split()
    token_set = set(tokens)
    phrase = " ".join(tokens)

    if (
        any(contains_phrase(tokens, p) for p in REFUSE_PHRASES)
        or bool(token_set & REFUSE_TOKENS)
        or phrase in ("na", "na ji")
        or ("not" in token_set and not contains_phrase(tokens, "not sure"))
    ):
        return "reject"

    if "?" in user_text or bool(token_set & AMBIGUOUS_TOKENS) or contains_phrase(tokens, "not sure"):
        return "ambiguous"

    if any(contains_phrase(tokens, p) for p in AFFIRM_PHRASES) or bool(token_set & AFFIRM_TOKENS):
        return "confirm"

    return "ambiguous"


# Treat LLM output as an untrusted proposal and enforce two-step explicit booking confirmation.
def validate_model_output(
    result: dict,
    available_slots: list[str],
    fallback_language: str,
    pending_slot: str | None,
    user_text: str,
) -> dict:
    validated = dict(result)

    safe_fallback = fallback_language if fallback_language in brain.VALID_LANGUAGES else "en"
    raw_lang = str(validated.get("language") or safe_fallback).lower().strip()
    validated["language"] = raw_lang if raw_lang in brain.VALID_LANGUAGES else safe_fallback

    parsed_score = brain.parse_valid_score(validated.get("lead_score"))
    raw_sentiment = validated.get("sentiment")
    sentiment_str = raw_sentiment.lower().strip() if isinstance(raw_sentiment, str) else ""
    if parsed_score is None or sentiment_str not in brain.VALID_SENTIMENTS:
        validated["fallback"] = True
        validated["lead_score"] = 20
        validated["sentiment"] = "neutral"
    else:
        validated["lead_score"] = parsed_score
        validated["sentiment"] = sentiment_str

    raw_extracted = validated.get("extracted")
    if not isinstance(raw_extracted, dict):
        raw_extracted = {}

    clean_extracted: dict[str, str | None] = {}
    for field, max_len in FIELD_MAX_LENGTHS.items():
        val = raw_extracted.get(field)
        if val is not None:
            text_val = str(val).strip()
            clean_extracted[field] = text_val[:max_len] if text_val else None
        else:
            clean_extracted[field] = None

    email_val = raw_extracted.get("email")
    if email_val is not None:
        email_str = str(email_val).strip()
        clean_extracted["email"] = email_str if EMAIL_REGEX.match(email_str) else None
    else:
        clean_extracted["email"] = None
    validated["extracted"] = clean_extracted

    confirmation = classify_booking_confirmation(user_text)
    slot_dt = validated.get("slot_datetime")
    is_free_slot = slot_dt in available_slots
    if not is_free_slot:
        validated["slot_datetime"] = None

    if validated.get("intent") == "book_appointment":
        if not is_free_slot:
            validated["intent"] = "enquiry"
            validated["reply"] = handlers.format_alternative_slots_reply(
                validated["language"], available_slots[:2]
            )
        elif slot_dt != pending_slot:
            if confirmation == "reject":
                validated["intent"] = "enquiry"
                validated["slot_datetime"] = None
                validated["clear_pending_slot"] = True
                validated["reply"] = handlers.format_booking_rejected_reply(validated["language"])
            else:
                validated["intent"] = "enquiry"
                validated["reply"] = handlers.format_slot_proposal_reply(
                    validated["language"], slot_dt
                )
        elif confirmation == "reject":
            validated["intent"] = "enquiry"
            validated["slot_datetime"] = None
            validated["clear_pending_slot"] = True
            validated["reply"] = handlers.format_booking_rejected_reply(validated["language"])
        elif confirmation != "confirm":
            validated["intent"] = "enquiry"
            validated["reply"] = handlers.format_slot_proposal_reply(
                validated["language"], pending_slot
            )
    elif pending_slot is not None and confirmation == "reject":
        validated["clear_pending_slot"] = True
        validated["slot_datetime"] = None

    return validated


# Serve the Zenvo voice-agent call page at the root URL.
@app.get("/", response_class=HTMLResponse)
def call_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "call.html")


# Serve the server-rendered CRM and sales analytics dashboard.
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    data = dashboard.get_dashboard_data()
    return templates.TemplateResponse(request, "dashboard.html", data)


# Serve the call details page with customer info, insights, sentiment timeline, and transcript.
@app.get("/dashboard/calls/{call_id}", response_class=HTMLResponse)
def call_detail_page(request: Request, call_id: int) -> HTMLResponse:
    call_data = dashboard.get_call_detail(call_id)
    if call_data is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return templates.TemplateResponse(request, "call_detail.html", {"call": call_data})


# Health check endpoint so monitoring or startup probes can verify the service is up.
@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


# Return the list of upcoming free 30-minute demo slots for the next 3 days.
@app.get("/api/slots")
def list_available_slots() -> dict:
    return {"slots": db.get_available_slots(days=3)}


# Start a new active voice session, find or create the lead, and return a localized greeting.
@app.post("/start_call")
def start_call(payload: StartCallRequest) -> dict:
    session_id = str(uuid.uuid4())
    lang_value = payload.language.value
    lead, is_returning = db.find_or_create_lead(payload.phone)
    db.create_session(session_id, lead["id"], lang_value)

    returning_lead = None
    if is_returning:
        returning_lead = {
            "name": lead.get("name"),
            "need": lead.get("need"),
        }

    greeting = build_greeting(lang_value, returning_lead)
    db.save_message(
        session_id=session_id,
        lead_id=lead["id"],
        role="agent",
        text=greeting,
        language=lang_value,
        sentiment=lead.get("sentiment") or "neutral",
        score=int(lead.get("score") or 0),
    )

    return {
        "session_id": session_id,
        "lead_id": lead["id"],
        "greeting": greeting,
        "language": lang_value,
        "returning_lead": returning_lead,
    }


# Run one turn: validate input, call brain, validate proposal, and save everything in one BEGIN IMMEDIATE transaction.
@app.post("/turn")
def handle_turn(payload: TurnRequest) -> dict:
    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("status") == "ended":
        raise HTTPException(status_code=409, detail="Session has already ended")
    if db.count_user_turns(payload.session_id) >= MAX_TURNS_PER_SESSION:
        raise HTTPException(status_code=429, detail="Maximum turn limit (60) reached for this session")

    lead_id = session["lead_id"]
    raw_language = payload.language.value if payload.language is not None else session["language"]
    turn_language = raw_language if raw_language in brain.VALID_LANGUAGES else "en"
    lead = db.get_lead(lead_id) or {}
    recent_messages = db.get_recent_messages(payload.session_id, limit=10)
    available_slots = db.get_available_slots(days=3)

    llm_result = brain.generate_turn_response(
        lead=lead,
        recent_messages=recent_messages,
        available_slots=available_slots,
        user_text=payload.text,
        language=turn_language,
    )
    validated_result = validate_model_output(
        llm_result,
        available_slots,
        turn_language,
        session.get("pending_slot"),
        payload.text,
    )

    conn = db.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh_row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (payload.session_id,)
        ).fetchone()
        if fresh_row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        fresh_session = dict(fresh_row)
        if fresh_session.get("status") == "ended":
            raise HTTPException(status_code=409, detail="Session has already ended")

        if (
            validated_result.get("intent") == "book_appointment"
            and validated_result.get("slot_datetime") != fresh_session.get("pending_slot")
        ):
            validated_result["intent"] = "enquiry"
            validated_result["reply"] = handlers.format_slot_proposal_reply(
                validated_result["language"], validated_result["slot_datetime"]
            )

        dispatched_result = router.dispatch_intent(
            validated_result["intent"],
            {
                "session_id": payload.session_id,
                "lead_id": lead_id,
                "lead": lead,
                "session_language": fresh_session["language"],
                "result": validated_result,
                "conn": conn,
            },
        )
        db.save_turn(
            session_id=payload.session_id,
            lead_id=lead_id,
            user_text=payload.text,
            user_language=turn_language,
            result=dispatched_result,
            conn=conn,
        )

        if dispatched_result.get("intent") == "book_appointment" or dispatched_result.get("clear_pending_slot"):
            new_pending_slot = None
        elif dispatched_result.get("slot_datetime") in available_slots:
            new_pending_slot = dispatched_result["slot_datetime"]
        else:
            new_pending_slot = fresh_session.get("pending_slot")

        if new_pending_slot != fresh_session.get("pending_slot"):
            db.update_pending_slot(payload.session_id, new_pending_slot, conn=conn)

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        print(f"[turn] Write transaction failed: error_type={type(exc).__name__}")
        raise HTTPException(status_code=500, detail="Failed to save turn")
    finally:
        conn.close()

    return {
        "reply": dispatched_result["reply"],
        "sentiment": dispatched_result["sentiment"],
        "lead_score": dispatched_result["lead_score"],
        "intent": dispatched_result["intent"],
        "language": dispatched_result["language"],
    }


# Finalize a call idempotently: return saved summary if already ended, else summarize and close session.
@app.post("/end_call")
def end_call(payload: EndCallRequest) -> dict:
    session = db.get_session(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.get("status") == "ended":
        saved = db.get_call_summary(payload.session_id)
        if saved is not None:
            return saved

    return summary.generate_and_save_summary(payload.session_id, session["lead_id"])
