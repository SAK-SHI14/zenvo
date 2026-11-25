# Zenvo — Interview Preparation Guide (44 Questions & Natural Answers)

> **Golden Rule for Every Answer**: Clearly separate what is **implemented & tested in this MVP** (FastAPI, SQLite, Groq/Gemini failover, Web Speech API + text fallback, mocked `pytest` + Node UI tests) from what is **proposed on paper** (`architecture.md`: Twilio/Exotel SIP, PostgreSQL, Redis, Kafka, vector RAG, RBAC auth) and what remains **unverified** (real physical mic/speaker hardware tests and live 100k load tests).

---

## 1. Business and Product Understanding

### Q1. What business problem does this project solve?
Manual outbound and inbound sales calls eat up significant employee hours on repetitive discovery and FAQ conversations. Answering the same questions about pricing, free trials, and data security drives up operational cost, and when clinics call outside business hours, high-intent leads are missed entirely. At the same time, different human reps qualify prospects inconsistently—producing noisy CRM pipelines—and manual note-taking or calendar scheduling creates administrative overhead after every call.

**Zenvo** solves this by acting as an autonomous first-line AI sales voice agent: it answers product questions consistently, captures caller contact/need/budget/timeline details, scores and qualifies leads (`new`/`warm`/`hot`), proposes and safely books demo slots, and generates structured post-call summaries (`objections`, `buying_signals`, `next_best_action`). That way, human sales reps only spend time on warm, qualified, ready-to-close buyers.

*Important distinction*: **The software system I built** is the autonomous AI sales voice agent + CRM pipeline; **the fictional SaaS product it sells** during calls is Zenvo's automated clinic appointment-reminder platform (Basic Rs 999, Pro Rs 2,499, Enterprise).

### Q2. Who would actually buy a sales agent like this?
Any high-volume B2B SaaS or SMB service team with repeatable inside-sales qualification—for example healthcare SaaS startups selling to clinics, ed-tech counseling desks, insurance renewal teams, or real-estate lead-qualification teams that receive dozens of repetitive inquiries daily and want instant 24/7 response before routing warm leads to human closers.

### Q3. Why build a voice interface instead of just a text chatbot?
In India especially, clinic owners and busy managers often prefer speaking naturally—mixing English and Hindi (Hinglish)—over typing long paragraphs into a web widget. A voice interface also highlights real engineering challenges that a simple chatbot avoids: turn latency budgets (`10.0s` deadline), concise 2-sentence phone-style responses, half-duplex audio turnaround (stopping the mic while speaking), and graceful text fallback when voice APIs aren't available.

### Q4. How would a company measure the business value of this agent?
Using four concrete metrics, three of which are already surfaced on `/dashboard`:
1. **Lead-to-qualified rate** (`Qualified Leads / Total Leads`, using our `score >= 40` or booked threshold).
2. **Appointment conversion rate** (`Appointments Booked / Total Leads`).
3. **Human rep time saved** (calls handled autonomously end-to-end without rep involvement, plus eliminated manual CRM data entry via auto-extracted fields and summaries).
4. **After-hours lead capture rate** (inquiries handled when human agents are offline).

### Q5. What are the limitations of replacing human sales representatives?
An AI agent works well for standardized discovery, FAQ answers, and scheduling, but it shouldn't replace human closers for complex custom Enterprise negotiations, emotional objections, legal/SLA redlines, or long-term relationship trust. That's why every call ends with a `next_best_action` designed for human follow-up, and unknown answers explicitly defer to a human specialist (`build_system_prompt` Rule 1).

### Q6. Why do you seed 3 clinic records at startup?
So a reviewer opening `/dashboard` immediately sees how KPIs, masked phone numbers, qualification statuses (`booked`, `hot`, `warm`), and detailed call transcripts behave—even before placing a live call—and `tools/reset_demo_db.py` lets us restore that exact baseline cleanly.

---

## 2. Architecture and Backend

### Q7. Why did you choose FastAPI?
Three practical reasons: automatic request schema validation via Pydantic (`StartCallRequest`, `TurnRequest` 1–500 chars, `PHONE_REGEX`, `LANGS` enum), automatic interactive Swagger docs at `/docs` that help inspect endpoints during demos, and native support for serving both JSON APIs and Jinja2 HTML templates + `StaticFiles` from one Python codebase.

### Q8. Why SQLite instead of PostgreSQL right now?
For a single-node MVP running locally on a laptop via `run.bat`, SQLite requires zero external services or Docker installation, supports ACID transactions (`BEGIN IMMEDIATE`), enforces unique constraints (`idx_active_slot`), and performs very well in WAL mode for low concurrency. In `architecture.md`, migrating to PostgreSQL is our first step for multi-worker horizontal scale.

### Q9. Why Jinja2 server-rendered templates instead of React for `/dashboard`?
React introduces Node build toolchains, bundling, state management, and CORS/auth complexity for no functional gain on a read-heavy CRM dashboard. With Jinja2 (`templates/dashboard.html`, `call_detail.html`), Python fetches aggregated SQLite metrics (`app/dashboard.py`), masks PII on the server (`mask_phone`), escapes HTML automatically to prevent XSS, and returns complete HTML in one hop. Even the `/call` page uses plain HTML + CSS + small vanilla JS with zero build steps.

### Q10. How do the frontend and backend communicate?
Over standard HTTP on the same origin (`127.0.0.1:8000`):
- Navigation routes (`GET /`, `GET /dashboard`, `GET /dashboard/calls/{id}`) return server-rendered HTML.
- Call lifecycle endpoints (`POST /start_call`, `POST /turn`, `POST /end_call`, `GET /api/slots`, `GET /health`) exchange JSON payloads via browser `fetch()`.

### Q11. Why does the application only need one server?
FastAPI mounts CSS/JS under `/static`, renders `call.html` and dashboard views via `Jinja2Templates`, and serves the REST endpoints inside the exact same Uvicorn process. That eliminates separate frontend/backend ports, simplifies startup to one `run.bat` click, and keeps CORS configured cleanly for local development.

### Q12. What happens under the hood when a call starts (`POST /start_call`)?
`start_call` validates `{phone?, language}` with Pydantic, generates a UUID `session_id`, calls `db.find_or_create_lead(phone)` to either reuse an existing lead or create a new `'new'` lead row, inserts an `'active'` `sessions` row, generates a localized greeting (`build_greeting`), saves that first `agent` message into `messages`, and returns `{session_id, lead_id, greeting, language, returning_lead}`.

### Q13. How does contextual memory work both within a call and across repeat calls?
**Within a call**, `/turn` loads the last 10 chronological messages (`db.get_recent_messages(session_id, limit=10)`) and attaches them to the OpenAI `messages` array. **Across repeat calls**, if the same phone number calls back, `db.find_or_create_lead(phone)` retrieves the saved `leads` row (`name`, `need`, `budget`, `timeline`), `build_greeting` welcomes them back with their stored name/need, and `build_system_prompt` injects `SAVED LEAD INFO` so the model skips asking duplicate questions.

---

## 3. AI and LLM Engineering

### Q14. Why use an LLM instead of purely rule-based scripts?
Real clinic owners ask open-ended questions, mix pricing with privacy concerns in one sentence, and switch between English, Hindi, and Hinglish mid-sentence. An LLM handles natural conversational phrasing and simultaneous entity extraction cleanly, while deterministic Python rules still own state transitions, slot booking, and database safety.

### Q15. Did you train or fine-tune your own model?
No—and for a startup MVP it wasn't necessary. We use pre-trained instruction-tuned models via OpenAI-compatible APIs (Groq `openai/gpt-oss-120b` primary, Google `gemini-3.5-flash-lite` optional fallback) combined with prompt grounding (`kb/*.md`), schema validation, and deterministic guardrails. Fine-tuning would only make sense later once we collect thousands of real clinic call transcripts.

### Q16. Why enforce structured JSON output instead of plain text?
Because every conversation turn must update **UI speech + CRM metrics + routing + extracted fields** simultaneously. Returning JSON (`reply`, `language`, `intent`, `sentiment`, `lead_score`, `extracted`, `slot_datetime`) lets Python drive UI badges, lead status transitions, slot confirmation logic, and database columns cleanly without brittle regex scraping over prose replies.

### Q17. Why use a single LLM request per conversational turn?
Latency. On a simulated voice call, users immediately feel delays above ~2–3 seconds. Running separate sequential LLM calls for intent detection, entity extraction, sentiment scoring, and reply generation would triple latency and cost. Combining them into one structured completion kept median live Groq latency around ~1.6s in our real run check (and ~0.9s before rate limits), well inside our `10.0s` turn deadline.

### Q18. How does the knowledge base work right now?
At startup (`app/brain.py`), `load_knowledge_base()` reads all six Markdown files in `kb/` (`pricing.md`, `features.md`, `faqs.md`, `onboarding.md`, `refund_policy.md`, `security.md`) into memory once (`KB_TEXT`), and `build_system_prompt` appends them directly into the system prompt so every turn avoids disk I/O and stays grounded.

### Q19. Is the current implementation actual vector-based RAG?
No—and I always state that transparently. Today our entire KB is only ~6 small Markdown files, so **full-document prompt injection** into memory gives zero retrieval errors and sub-millisecond prompt assembly without vector DB complexity.

### Q20. How would you implement true production RAG if the knowledge base grew to hundreds of documents?
1. Chunk documents (by section headings + token size ~300–500 tokens).
2. Generate embeddings and store chunks + tenant metadata in a vector index (`pgvector` in PostgreSQL or Qdrant).
3. Embed the caller's latest query + recent context on `/turn`, retrieve top-k chunks with metadata filtering (e.g., clinic plan/region), optionally rerank, and inject only those top chunks into the system prompt—plus cite section IDs for auditability.

### Q21. How are sentiment and lead scores calculated?
During each turn, the LLM estimates `sentiment` (`positive|neutral|negative`) and `lead_score` (`0..100`) alongside `reply`, validated strictly by `parse_valid_score()` (rejecting strings/bools/`NaN`/out-of-range numbers). Python maps scores via `compute_lead_status()`: `<40` = `new`, `>=40` = `warm`, `>=70` = `hot`, and confirmed booking preserves `booked`. If an LLM call fails (`fallback=True`), `save_turn` intentionally skips `merge_lead_fields` so lead scores stay protected. At `/end_call`, `summary.py` builds a deterministic fallback score/timeline from DB rows whenever the analyst model is unavailable.

### Q22. How does multilingual support (`en`, `hi`, `hinglish`) work?
1. **Enum & DB**: `LANGS` in `app/db.py` restricts languages across Pydantic, SQLite `CHECK` constraints, and Python coercion to `{"en", "hi", "hinglish"}`. Punjabi (`pa`) and unsupported codes were cleanly removed.
2. **Prompting**: `build_system_prompt` tells the LLM to match the caller's language and reply in Hinglish (`"language": "hinglish"`) if an unsupported language appears.
3. **Coercion**: Both `normalize_llm_result` and `validate_model_output` coerce any unexpected language back to the session language or `"en"`.
4. **Browser Mapping**: `call.js` maps `en -> en-IN`, `hi -> hi-IN`, and `hinglish -> en-IN`.

### Q23. Why use both Groq (`openai/gpt-oss-120b`) and Gemini (`gemini-3.5-flash-lite`)?
Groq delivers very fast inference (`~0.9s–1.6s` observed median latency) via an OpenAI-compatible endpoint, making it great for conversational turns, though free-tier quotas can hit `429 RateLimitError`. Configuring Gemini as an optional secondary provider gives resilience if the primary experiences outages or strict rate limits—verified via `tools/test_failover.py` and mocked tests. Because earlier Gemini diagnostics revealed intermittent HTTP `503` errors, we added bounded `500/502/503/504` retries (`SECONDARY_MAX_ATTEMPTS = 3`, `1.0s` delay) before dropping to the scripted template.

### Q24. What happens when an LLM returns invalid JSON or becomes unavailable?
First, `safe_parse_json` strips markdown fences (` ```json `) and rescues embedded `{...}` JSON objects. If parsing or `normalize_llm_result` still fails, `generate_turn_response` retries primary up to 2 attempts (`attempt in range(2)`) inside `TURN_DEADLINE_SECONDS = 10.0` (with `429` `Retry-After` backoff capped at `3.0s`, plus falling back without `response_format` on `BadRequestError`). If primary fails, it tries secondary Gemini with bounded `5xx` retries; if both fail, `fallback_reply(language)` returns a polite multilingual message with `fallback=True` so the call never crashes.

### Q25. How do you prevent the model from executing unauthorized actions?
Two layers of defense: **first**, the LLM has zero database access, zero tool-calling permissions, and zero code execution—it only returns a JSON *proposal*. **Second**, `validate_model_output()` in `app/main.py` strips any slot not present in real SQLite free slots, checks explicit confirmation words via `classify_booking_confirmation()`, truncates text fields (`FIELD_MAX_LENGTHS`), validates emails by regex, and lets `router.dispatch_intent()` execute **only** registered handlers (`book_appointment`).

---

## 4. Voice and Frontend

### Q26. How does speech-to-text (STT) work?
In Chrome, `static/call.js` instantiates `window.SpeechRecognition || window.webkitSpeechRecognition` (`continuous = false`, `interimResults = true`) mapped to `en-IN` or `hi-IN`. Interim audio updates `#mic-hint`, and when `event.results[i].isFinal` fires with non-empty text, JS aborts the recognizer (`discardListening`) and sends the transcript to `sendTurn(cleaned)` -> `POST /turn`.

### Q27. How does text-to-speech (TTS) work?
`speakReply(text, lang)` in `static/call.js` uses `window.speechSynthesis`. `pickVoiceForLanguage()` searches `speechSynthesis.getVoices()` for matching `en-IN` or `hi-IN` voices, wraps `text` in a `SpeechSynthesisUtterance` (`rate = 1.0`), cancels any queued utterance, attaches `onend`/`onerror` plus a dynamic `speechWatchdogTimer` (8–20s), and speaks aloud.

### Q28. Is this real telephony?
No—and the UI explicitly states *"Simulated call: browser STT/TTS stand in for telephony"* right in the header (`templates/call.html`). Browser Web Speech API lets us demonstrate real-time voice interaction cleanly on a laptop with zero paid telephony setup, while keeping the backend cleanly decoupled around `/start_call`, `/turn`, and `/end_call` for future telephony adapters.

### Q29. How do you prevent the agent from hearing its own voice?
Through a strict **half-duplex state lock** (`IDEA 4` in `call.js`): right at the top of `speakReply()`, JS calls `discardListening()` (`recognition.abort()`), sets `isSpeaking = true`, disables the mic button (`refreshButtons()`), and **only** restarts `startListening()` inside `completeSpeech()` after speech finishes—AND `startListening()` also waits for any prior `recognition.onend` teardown (`isRecognizing` + `pendingRestartAfterTeardown`) before starting Chrome's recognizer again.

### Q30. What is an asynchronous callback, and why do tokens matter here?
An asynchronous callback is a function executed *later* when an external operation finishes—like `fetch("/turn")` resolving, Chrome firing `recognition.onresult`, or `utterance.onend` firing. If a user hangs up or starts a brand-new call before an old network response or delayed `onend` arrives, that stale callback could overwrite UI state or accidentally submit an old phrase. Incrementing `callToken`, `recognitionAttemptId`, `activeRecognitionId`, and `speechAttemptId` lets callbacks discard themselves whenever tokens don't match (`if (myCallToken !== callToken) return;`).

### Q31. Why provide a text fallback alongside voice?
Three reasons: (1) Non-Chrome browsers, missing OS voices, or denied mic permissions don't block the caller (`#text-input` calls the exact same `sendTurn()` function); (2) headless automated UI verification (`node tests/test_call_ui.js` and Chrome headless checks) runs reliably without physical audio hardware; and (3) noisy environments don't derail a live demo. Notice physical microphone/speaker hardware itself remains unverified in CI.

---

## 5. Database and Reliability

### Q32. How are double bookings prevented?
Three layered controls work together:
1. **Two-Step App Logic**: Turn 1 only saves `sessions.pending_slot`; Turn 2 requires `classify_booking_confirmation == "confirm"`, verifies the requested slot matches `fresh_session["pending_slot"]`, and confirms the slot is still in `available_slots`.
2. **Database Constraint**: Partial unique index `idx_active_slot ON appointments(slot_datetime) WHERE status = 'confirmed'` makes duplicate confirmed rows impossible at the SQLite engine level.
3. **Savepoint Rollback**: `db.book_slot()` inserts inside `SAVEPOINT book_slot`. On `sqlite3.IntegrityError`, it rolls back only the savepoint, fetches the next 2 open slots (`format_alternative_slots_reply`), and preserves the turn messages cleanly. *(Tested in `test_double_booking`.)*

### Q33. Why use `BEGIN IMMEDIATE`?
In SQLite WAL mode, a plain `BEGIN` (deferred transaction) starts with a read lock and tries to upgrade to a write lock later during `INSERT`/`UPDATE`. If two concurrent requests upgrade at the same time, SQLite immediately aborts one with `SQLITE_BUSY` without waiting for `timeout`. `BEGIN IMMEDIATE` acquires the write lock up front at transaction start, so competing `/turn` or `/end_call` requests queue politely up to the 10-second `timeout`.

### Q34. What is idempotency, and where did you implement it?
An operation is idempotent if calling it multiple times produces the same final state and response as calling it once. We implemented it on `POST /end_call`: if `session["status"] == "ended"`, `app/main.py` immediately returns `db.get_call_summary(session_id)` without running the analyst LLM again. And if two `/end_call` calls race before commit, `calls.session_id UNIQUE` raises `IntegrityError` inside `db.finalize_call()`, which rolls back and returns the winning call's saved summary. *(Tested in `test_end_call_twice`.)*

### Q35. What happens if a customer refuses an appointment?
`classify_booking_confirmation(user_text)` matches `REFUSE_PHRASES` / `REFUSE_TOKENS` (including English, Hindi Devanagari like `"नहीं"`, and Roman Hinglish like `"mat karo"`). `validate_model_output` downgrades intent to `"enquiry"`, sets `clear_pending_slot = True`, returns `format_booking_rejected_reply()` politely, and `handle_turn` clears `sessions.pending_slot` back to `NULL` so future messages can't accidentally confirm it.

### Q36. How do you prevent invalid or fallback LLM responses from corrupting CRM data?
Two ways:
- Invalid schema outputs (missing `reply`, bad `sentiment`, `NaN`/bool/string `lead_score`) are rejected by `normalize_llm_result` / `validate_model_output` and marked `fallback=True`.
- In both `db.save_turn()` (`if not is_fallback: merge_lead_fields(...)`) and `db.finalize_call()` (`if not is_fallback: merge_lead_fields(...)`), fallback responses **never** overwrite existing `leads.score`, `leads.sentiment`, or extracted customer fields (`test_fallback_does_not_change_lead`). Also `merge_lead_fields` ignores blank/None fields so partial extractions never erase saved details.

### Q37. How are concurrent `/turn` and `/end_call` requests handled?
On the backend, `handle_turn()` re-reads `fresh_session` inside `BEGIN IMMEDIATE` right before dispatching intent and committing—if `/end_call` committed while the LLM call was in flight, `fresh_session["status"] == "ended"` raises `HTTP 409 Conflict` cleanly without saving a message after call end. And `/turn` after end returns `409` (`test_turn_after_end`). On the frontend, `isRequestPending` disables controls and `call.js` handles `409` gracefully.

---

## 6. Production, Security and Scalability

### Q38. How would you scale this to support 100,000 daily users?
Per the math in `architecture.md`: 100,000 daily 30-second calls = `50,000` call-minutes/day (`~250` peak concurrent calls assuming 3x burst over a 10h business day) and `~110,000` daily LLM requests (`~15–20` req/s burst). To handle that beyond one SQLite laptop process:
1. Convert turn orchestration to **async workers** behind a load balancer (`asyncpg` + `AsyncOpenAI` connection pooling).
2. Replace SQLite with **PostgreSQL** (`UNIQUE` booking constraint preserved) + connection pooler (PgBouncer).
3. Put short-lived session state (`pending_slot`, rate-limit counters) in **Redis**.
4. Offload post-call summaries and CRM webhook sync to **Kafka / worker queues** so hangup latency stays near zero.
5. Add **vector RAG** (`pgvector`) if KB grows, plus **prompt caching** on static KB prefixes.

### Q39. Why specifically introduce Redis, Kafka, and PostgreSQL in production?
- **PostgreSQL**: Supports true concurrent multi-worker writes, ACID row locks, read replicas for `/dashboard` analytics, and managed backups/encryption at rest—unlike single-writer file SQLite.
- **Redis**: Gives sub-millisecond reads/writes for active session state (`pending_slot`), per-caller rate limiting, and caching open appointment slots without hitting Postgres on every audio packet.
- **Kafka (or task queue)**: Decouples slow asynchronous work—post-call LLM summarization, CRM webhook sync (HubSpot/Salesforce/Zoho), WhatsApp confirmations, and audit logs—so users never block waiting for post-call jobs.

### Q40. How would you integrate Twilio (or Exotel) and an external CRM?
- **Telephony (Proposed)**: Replace Chrome Web Speech with Twilio Media Streams / Exotel WebSocket streaming 8kHz audio to streaming ASR (Deepgram/Whisper), feeding text into our existing `/turn` brain contract, and streaming low-latency neural TTS back over audio chunks with barge-in support.
- **CRM (Proposed)**: Dispatch an idempotent webhook/event (`call_id` idempotency key) to HubSpot/Zoho/Freshsales inside an async Kafka worker after `finalize_call()` commits, retrying transient `5xx` errors cleanly.

### Q41. How do you protect customer information today, and what is still needed for production?
**Implemented today**:
- Phone numbers are masked (`mask_phone()` -> `+91******2233`) in Python before HTML reaches `/dashboard` or `/dashboard/calls/{id}`.
- Raw `phone` and `email` are excluded from `SAVED LEAD INFO` sent to third-party LLM prompts (`build_system_prompt`).
- Logs omit API keys, customer phone numbers, and raw prompt text.
- XSS is mitigated via Jinja2 auto-escaping and `textContent` DOM insertion (`call.js`).
- CORS restricts browser origins to localhost/`127.0.0.1`.

**Needed for production (Proposed in `architecture.md`)**:
- Authentication / RBAC (JWT or OAuth2) on `/dashboard` and API endpoints (currently open locally).
- Strict secret manager (Vault / KMS) instead of `.env` files, HTTPS/TLS termination, encrypted DB volumes, and DPDP consent + audit logging.

### Q42. How would you protect against prompt injection from a malicious caller?
Today we already bound caller messages to `500` characters (`TurnRequest`), treat every LLM output strictly as an untrusted JSON proposal (`validate_model_output`), never grant tool/SQL privileges to the prompt, and cap extracted field lengths (`FIELD_MAX_LENGTHS`). In production, I would also wrap user utterances in explicit delimiters, strip role-mimicking control tokens (`system:`, `<|im_start|>`), run lightweight moderation/injection classifiers before LLM calls, and add output policy filters before TTS playback.

### Q43. What are the major operating costs at scale?
As parameterized in `architecture.md`, **Cost_daily = Cost_telephony + Cost_STT_TTS + Cost_LLM + Cost_infra**. In real deployments, **telephony minutes + streaming STT/TTS audio** usually exceed LLM token cost for 30s voice calls, followed by LLM input tokens (system prompt + KB + history repeated over ~10 turns/call, which **prompt caching** cuts sharply), and finally cloud DB/Redis/compute hosting.

### Q44. What would you improve first if given one more month?
Three concrete upgrades, prioritized by impact:
1. **Authentication + RBAC** on `/dashboard` and `/api/*` so multi-user clinics only view their own clinic leads.
2. **Async FastAPI + Streaming Audio Pipeline** (`AsyncOpenAI` + streaming TTS sentence-by-sentence) to cut perceived voice latency under ~800ms.
3. **Real PostgreSQL + Automated End-to-End CI** covering streaming WebSocket turns, Twilio webhook signatures, and live hardware audio smoke checks.
