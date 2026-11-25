# Zenvo — Autonomous AI Sales Voice Agent: Project Report

## 1. Problem Statement & Solution
Healthcare clinics lose appointments when inbound pricing, trial, and onboarding inquiries go unanswered outside staff hours. **Zenvo** is a multilingual autonomous AI sales voice agent built to handle inbound clinic inquiries end to end. It detects customer language (`en`, `hi`, `hinglish`), answers product questions from a grounded Markdown knowledge base (`/kb/*.md`), tracks live sentiment and lead qualification scores, proposes and confirms demo slots, generates post-call sales intelligence, and surfaces records in a server-rendered CRM dashboard.

## 2. Tech Stack & Implemented Architecture
The application runs as a single **FastAPI** (`0.165.0`) process served by **Uvicorn** (`0.54.0`) backed by **SQLite** with WAL journaling enabled in `app/db.py`. The browser voice console (`templates/call.html`, `static/call.js`, `static/call.css`) uses native **Web Speech API** (`SpeechRecognition` / `SpeechSynthesis`) with `en-IN` and `hi-IN` locale mapping plus a full text fallback. The CRM dashboard (`app/dashboard.py`, `templates/dashboard.html`, `templates/call_detail.html`) is server-rendered with **Jinja2** (`3.1.6`) and pure CSS. AI inference uses the **OpenAI-compatible SDK** (`openai 3.59.0`) targeting Groq (`openai/gpt-oss-120b`) as primary and Google Gemini (`gemini-3.5-flash-lite`) as fallback, bounded by an explicit `7.5s` turn deadline and `3.5s` summary deadline.

## 3. End-to-End Workflow
1. `POST /start_call` normalizes the phone number, detects returning leads, creates a `sessions` row, and returns a localized greeting.
2. Each `POST /turn` validates input (`1–500` chars), appends the message, checks active session state (`409` if ended, `429` at 60 turns), and executes LLM reasoning with recent conversation history and available slots.
3. Responses are coerced to schema (`reply`, `sentiment`, `lead_score`, `intent`, `language`, `slot_datetime`), persisted, and spoken via TTS.
4. `POST /end_call` marks the session ended, computes deterministic heuristic scores plus LLM-assisted executive insights (`app/summary.py`), and logs a completed call row visible at `/dashboard`.

## 4. Key Engineering Decisions
- **Two-Step Slot Confirmation**: `app/main.py` stores a valid proposed slot in `sessions.pending_slot` on turn 1 and commits an `appointments` row only on turn 2 after `classify_booking_confirmation` confirms user intent, preventing accidental bookings.
- **Resilience & Failover**: Primary timeouts or transient errors fail over to Gemini with bounded `500/502/503/504` retries (`1.0s` delay, max 3 attempts within remaining deadline), followed by a scripted multilingual fallback template (`app/handlers.py`).
- **Privacy & Safety**: Prompts are bounded (`kb/*.md` loaded in full into system prompts rather than vector RAG), HTML output is Jinja2-escaped and DOM-built via `textContent`, and phone numbers are masked (`******NNNN`) in the CRM UI.

## 5. Verification Results
After final backend and frontend implementation, automated suites passed cleanly: `37 passed` in `python -m pytest -q` and `4/4` UI regression checks in `node tests/test_call_ui.js`, alongside headless Chrome verification on `/`, `/dashboard`, and `/dashboard/calls/1`. In earlier live provider checks, Groq `openai/gpt-oss-120b` passed `7/7` scenario checks with `p50 1.64s` (`max 2.46s`), while Gemini `gemini-3.5-flash-lite` diagnostics revealed intermittent HTTP `503` responses motivating the bounded retry layer.

## 6. Scalability, Cost, Security & Limitations
Scaling to 100,000 daily callers (`~50,000` call-minutes/day; `~250` peak concurrent calls; `~110,000` daily LLM requests) requires migrating from synchronous SQLite/HTTPX workers to PostgreSQL, Redis, async queues, telephony SIP/WebRTC gateways, and RBAC authentication as detailed in `architecture.md`. Token costs scale linearly per turn plus prompt caching potential. Current MVP limitations include unauthenticated local endpoints, browser-dependent speech hardware not verified on physical microphones here, and single-node synchronous execution.
