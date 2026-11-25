# Zenvo — Live Demonstration & Verification Guide

This executable script walks you through preparing, demonstrating, and troubleshooting **Zenvo** during a live interview. Every action, sentence, UI element ID, database behavior, and fallback check matches the implemented code.

**Fictional Demo Persona Used Throughout**:
- **Clinic Name**: CarePlus PolyClinic
- **Doctor / Caller Name**: Dr. Meera Nair
- **Phone Number**: `+919900112233`

---

## Scenario A — Pre-Demo Preparation

Follow these steps **15 minutes before** the interview:

1. **Run the Read-Only Preflight Checker**
   - **Action**: Open a terminal in `Project-Sakshi-Placement` and run:
     ```bat
     .venv\Scripts\python.exe tools\preflight.py
     ```
   - **Expected**: `Status: READY - all preflight checks passed.` (Zero `[FAIL]` lines; API key values are never printed).
   - **If it fails**: Follow the printed `[FAIL]` item—most commonly a missing key in `.env` or a missing dependency in `.venv`.

2. **Verify Automated Offline Tests**
   - **Action**: Run:
     ```bat
     .venv\Scripts\python.exe -m pytest -q
     node tests\test_call_ui.js
     ```
   - **Expected**: All `pytest` tests pass cleanly (`37 passed` in baseline) and `tests/test_call_ui.js` reports `4/4` passing checks.
   - **If Node.js is absent**: Skip `node tests\test_call_ui.js` and mention Python `pytest` covers backend contracts cleanly.

3. **Reset the Demo Database to Clean Seed Baseline**
   - **Action**: Ensure no server is running on port 8000, then run:
     ```bat
     .venv\Scripts\python.exe tools\reset_demo_db.py --rebuild
     ```
   - **Expected**: Prints backup path `zenvo.db.bak.<timestamp>`, deletes runtime test rows, and re-seeds 3 baseline leads (`Dr. Ananya Sharma`, `Rajesh Verma`, `Dr. Harpreet Gill`) plus `1` confirmed appointment (`slots[0]`).
   - **Explain to interviewer**: "Our demo script backs up the existing SQLite file cleanly and resets to 3 seeded clinic leads so our metrics start from a realistic, reproducible baseline."

4. **Start the Application Using `run.bat`**
   - **Action**: Run `.\run.bat` (or double-click `run.bat`).
   - **Expected**: `[1/4] Using existing .venv environment.`, `[3/4] Initializing SQLite database...`, `[4/4] Starting Zenvo FastAPI server...` serving `http://127.0.0.1:8000/`.
   - **If port 8000 is busy**: Close the previous terminal holding Uvicorn or run `netstat -ano | findstr :8000` and terminate the stale PID.

5. **Check Dashboard Baseline**
   - **Action**: Open `http://127.0.0.1:8000/dashboard` in Google Chrome.
   - **Expected Baseline KPIs**: Total Leads = `3`, Completed Calls = `3`, Qualified Leads = `3` (scores `88`, `72`, `48` are all `>= 40`), Appointments Booked = `1`, Avg Lead Score = `69.3`, Conversion Rate = `33.3%`.

6. **Verify Chrome Microphone Permission & Installed TTS Voices**
   - **Action**: Open `http://127.0.0.1:8000/` in Chrome. Click the padlock/tune icon in Chrome's address bar -> confirm **Microphone** is set to **Allow** for `http://127.0.0.1:8000`.
   - **Voice check**: Press `F12` -> **Console** -> run:
     ```js
     speechSynthesis.getVoices().filter(v => v.lang.includes("IN")).map(v => `${v.name} (${v.lang})`)
     ```
   - **Expected**: Lists installed `en-IN` and/or `hi-IN` voices (e.g., Windows Microsoft Ravi/Heera or Google Hindi).
   - **If voices list is empty or mic fails**: You still have full **Text Fallback** via the `#text-input` box (`templates/call.html`), and `#voice-note` notifies cleanly without breaking the call.

---

## Choosing a Safe Future Slot Date on Demo Day

`app/db.py` (`generate_candidate_slots`) generates **30-minute slots from `10:00` to `16:30` for days `today + 1`, `today + 2`, and `today + 3`** formatted as `YYYY-MM-DD HH:MM`.
Because `app/seed.py` books the **very first candidate slot** (`slots[0]`, i.e., **tomorrow at `10:00`**) for `Dr. Ananya Sharma`, before starting Scenario B open a browser tab or terminal to check `http://127.0.0.1:8000/api/slots`:
- Pick any free slot from that JSON list—for example, **tomorrow at `11:30`** or **`14:00`**.
- Keep **`10:00` tomorrow** untouched so you can intentionally trigger a **double-booking collision test** in Scenario E!

---

## Scenario B — Complete Successful Sales Conversation (17 Steps)

> **Important Note**: Never promise exact word-for-word LLM phrasing or exact integer scores—explain the *expected semantic outcome*, schema updates, and DB transitions instead.

### Step 1 — Start a Call
- **Click / Enter**: On `http://127.0.0.1:8000/`, select Language = `English (en-IN)`, enter Phone = `+919900112233`, click **Start Call** (`#start-btn`).
- **Expected Agent Behavior**: Returns a welcome greeting for a new clinic caller and speaks it aloud (`Speaking` badge -> `Listening` or `Idle`).
- **UI / DB Change**: Creates a new `leads` row (`phone='+919900112233'`, `score=0`, `status='new'`), an `active` row in `sessions`, and an `agent` row in `messages`. `#start-btn` disables, `#end-btn` & `#text-input` enable, first agent bubble renders.
- **Explain to Interviewer**: "Calling Start Call creates a persistent lead and active session UUID in SQLite, then pauses the microphone while speaking the greeting so the agent never hears itself."
- **If it fails**: Verify server is running and `.env` is readable; retry clicking Start Call.

### Step 2 — Product Enquiry & KB Grounding
- **Speak or Type**: `"Hi, I run CarePlus PolyClinic. How does Zenvo reduce patient no-shows?"`
- **Expected Agent Behavior**: Explains automated SMS, WhatsApp, and AI voice reminders (24h and 2h before visits, up to 60% reduction from `kb/features.md`) in <= 2 phone-style sentences.
- **UI / DB Change**: Transcript shows You + Zenvo bubbles; Sentiment updates (`neutral` or `positive`); Lead Score rises above `0`; `messages` gains 2 rows.
- **Explain to Interviewer**: "All answers are grounded in six Markdown KB files loaded at startup—the prompt instructs the model not to invent features outside `/kb/*.md`."
- **If it fails**: If a network error shows, your draft stays inside `#text-input`—simply press **Send** again.

### Step 3 — Accurate Basic, Pro & Enterprise Pricing
- **Speak or Type**: `"Can you tell me the pricing for Basic, Pro, and Enterprise plans?"`
- **Expected Agent Behavior**: Accurately quotes **Basic at Rs 999/month** (500 reminders, 1 clinic), **Pro at Rs 2,499/month** (2,500 reminders, AI voice, 3 clinics), and **Enterprise with custom pricing**, optionally noting `18% GST` / annual 2 months free (`kb/pricing.md`).
- **UI / DB Change**: Both messages logged; `lead_score` increases as pricing curiosity signals buying intent.
- **Explain to Interviewer**: "Financial numbers come straight from `kb/pricing.md` rather than model memory."
- **If it fails**: If only two plans fit in 2 sentences, follow up with `"What does Enterprise include?"`.

### Step 4 — Security, Refund & Free Trial KB Grounding
- **Speak or Type**: `"We care about patient privacy—where is data stored, and do you offer a free trial and refund policy?"`
- **Expected Agent Behavior**: Cites Mumbai Indian cloud hosting (`TLS 1.3` / `AES-256` from `kb/security.md`), **14-day free trial** (up to 50 free reminders from `kb/faqs.md`), and **14-day 100% money-back guarantee** (`kb/refund_policy.md`).
- **UI / DB Change**: Transcript updates; sentiment remains positive/neutral; no hallucinations.
- **Explain to Interviewer**: "Notice how security residency and billing terms stay faithful to `kb/security.md` and `kb/refund_policy.md`."
- **If it fails**: Ask specifically: `"Where is data stored?"`.

### Steps 5, 6, 7, 8 — Extract Name, Budget, Business Requirement & Timeline
- **Speak or Type** (can be split naturally across 1–2 turns):
  `"I'm Dr. Meera Nair from CarePlus PolyClinic. We need WhatsApp and voice reminders for 2 doctors with a Rs 2,500 monthly budget, and we want to launch this week."`
- **Expected Agent Behavior**: Uses `intent: "update_crm"`, extracts structured fields (`name: "Dr. Meera Nair"`, `budget: "Rs 2,500..."`, `need: "...WhatsApp and voice reminders..."`, `timeline: "this week"`), acknowledges cleanly, and invites next steps.
- **UI / DB Change**: `db.merge_lead_fields()` updates the `leads` row in SQLite (`name`, `budget`, `need`, `timeline` trimmed to `FIELD_MAX_LENGTHS`).
- **Explain to Interviewer**: "The LLM returns structured JSON every turn so CRM entity extraction happens seamlessly inside dialogue, not as a separate pipeline call."
- **If it fails**: Check that the message stays under 500 characters; if any field wasn't captured, restate just that detail naturally.

### Steps 9 & 10 — Sentiment Changes & Lead Qualification Score Updates
- **Speak or Type**: `"Wait, Rs 999 extra charges or setup complexity sounds annoying if reception staff struggle with it."`
  -> *Wait for reply (note onboarding 30-min setup / zero setup fee)* -> then send:
  `"Okay, 30-minute onboarding with no setup fee sounds great! Let's get started."`
- **Expected Agent Behavior**: First message drives `sentiment` toward `negative`/`neutral` while addressing the objection (`kb/onboarding.md`, `kb/pricing.md`); second message shifts `sentiment` to `positive` and boosts `lead_score` (`>= 40` -> `warm`, or `>= 70` -> `hot`).
- **UI / DB Change**: `#sentiment-value` pill switches colors cleanly; `#score-fill` bar animates upward; `leads.status` transitions via `compute_lead_status()`.
- **Explain to Interviewer**: "Every turn records sentiment and score on both the message row (building the sentiment timeline) and the cumulative lead profile."
- **If it fails**: Scores vary by LLM estimate; focus interviewer attention on the directional shift and threshold (`40+` Warm, `70+` Hot).

### Steps 11, 12 & 13 — Appointment Proposal, Explicit Confirmation & Successful Booking
- **Step 11 (Proposal) — Speak or Type**: `"Can we schedule a product demo for tomorrow at 11:30 AM?"` *(use an open slot checked earlier via `/api/slots`)*.
  - **Expected**: Agent confirms availability (`format_slot_proposal_reply`: *"Would you like me to confirm this slot for you?"*).
  - **DB Change**: `sessions.pending_slot` is set to `'YYYY-MM-DD 11:30'`. **Zero rows added to `appointments` yet!**
- **Step 12 & 13 (Explicit Confirmation & Booking) — Speak or Type**: `"Yes, please confirm that slot."`
  - **Expected**: `classify_booking_confirmation` returns `"confirm"`, backend matches `slot_datetime == pending_slot`, `handle_booking()` inserts a `'confirmed'` row via `SAVEPOINT book_slot`, and replies with fixed confirmation (`"Done! Your demo is booked for ..."`).
  - **DB Change**: New row in `appointments`, `leads.status = 'booked'`, `sessions.pending_slot = NULL`.
- **Explain to Interviewer**: "Booking is intentionally two-step and deterministic. The LLM can only *propose* a slot; Python validates the word confirmation and acquires `BEGIN IMMEDIATE` + a unique partial index `idx_active_slot` before committing."
- **If it fails**: Ensure you used an exact free date/time from `/api/slots`. If the LLM returns a slightly wrong minute, specify `"YYYY-MM-DD 11:30"` explicitly.

### Steps 14 & 15 — End Call & Generated Summary Insights
- **Speak or Type** (or Click `#end-btn` directly): `"Thank you, please end the call now."`
  - **Wait or Click**: If `intent == "end_call"`, `call.js` sets `pendingAutoEnd = true`, waits for TTS goodbye to finish, then calls `POST /end_call`. Or click **End Call** (`#end-btn`) directly.
- **Expected Agent Behavior**: `/end_call` runs `summary.generate_and_save_summary()`, writes one `calls` row, marks `sessions.status = 'ended'`.
- **UI Change**: `#summary-card` reveals **Summary**, **Objections** (e.g., reception setup concern), **Buying Signals** (pricing/trial questions, demo booked), **Next Best Action**, **Overall Sentiment**, and **Final Lead Score**.
- **If it fails**: `#end-btn` remains clickable on network errors; click **End Call** again (backend is idempotent).

### Steps 16 & 17 — CRM Dashboard Updates, Detail View & Sentiment Timeline
- **Click**: **Open Dashboard** button inside `#summary-card` (`href="/dashboard"`).
- **Expected UI Change**:
  - KPIs increment: Total Leads `4`, Completed Calls `4`, Appointments Booked `2`.
  - **Leads** table displays `Dr. Meera Nair` at the top with masked phone `+91******2233`, language `en`, status `booked`.
  - **Appointments** table lists Dr. Meera Nair's confirmed slot.
- **Click**: Under **Recent Calls**, click **View Details** for `Dr. Meera Nair` (`/dashboard/calls/4` or newest call ID).
- **Expected UI Change**: Displays masked customer profile, Objections, Buying Signals, Next Best Action, color-coded **Sentiment Timeline** steps, and complete speaker-labeled **Conversation Transcript**.
- **Explain to Interviewer**: "All dashboard views are Jinja2 server-rendered directly from SQLite—raw phone numbers are masked in Python (`mask_phone`) before HTML ever leaves the server."

---

## Scenario C — Contextual Memory (Returning Caller)

1. **Click / Enter**: Navigate back to `/` (`New Call`), choose `English (en-IN)`, enter the **same phone number** `+919900112233`, click **Start Call**.
2. **Expected UI & Agent Behavior**:
   - Yellow/green `#returning-note` appears: `"Welcome back, Dr. Meera Nair! Last noted need: ..."`
   - Opening greeting automatically greets `Dr. Meera Nair` and references her previously stored clinic need (`build_greeting`).
3. **Speak or Type**: `"Hi, I forgot what plan fits our Rs 2,500 budget—can you remind me?"`
4. **Expected Agent Behavior**: Recommends **Pro Plan (Rs 2,499/month)** tailored to her stored `2 doctors / WhatsApp & voice reminders` requirement without asking *"What is your name?"* or *"How many clinics do you have?"* again.
5. **Explain to Interviewer**: "Every turn injects non-PII `SAVED LEAD INFO` (`name`, `need`, `budget`, `timeline`) from `leads`—note we intentionally omit raw phone and email from the LLM prompt."
6. **End Call**: Click **End Call** cleanly.

---

## Scenario D — Multilingual Capabilities

Test each language deliberately in a fresh call (or mid-call text turns):

| Test | Language Dropdown | Exact Sentence to Speak / Type | Expected Behavior |
| :--- | :--- | :--- | :--- |
| **English** | `English (en-IN)` | `"What is the cost of extra SMS reminders?"` | Replies in English mentioning `Rs 1` per additional SMS/WhatsApp (`language="en"`). |
| **Hindi (Devanagari)** | `Hindi (hi-IN)` | `"बेसिक प्लान का क्या प्राइस है और क्या इसमें फ्री ट्रायल मिलता है?"` | Replies in Hindi (Devanagari/Hindi phrasing) quoting Rs 999/month and 14-day free trial (`language="hi"`). |
| **Hinglish (Roman)** | `Hinglish (en-IN)` | `"Hinglish mein batao, extra reminder kitne ka padta hai aur setup time kitna hai?"` | Replies naturally in Roman Hinglish quoting Rs 1 and 30-min setup (`language="hinglish"`). |
| **Unsupported Language (e.g. Punjabi / Tamil)** | `English (en-IN)` | `"ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਨੂੰ ਜ਼ੇਨਵੋ ਦੀ ਪ੍ਰਾਈਸਿੰਗ ਦੱਸੋ।"` *(or any non-en/hi/hinglish text)* | `build_system_prompt` instructs LLM to reply in **Hinglish** and set `"language": "hinglish"`. Even if an LLM returns an unsupported code like `"pa"`, `normalize_llm_result` / `validate_model_output` silently coerces it back to the session language (`"en"`). |

**Crucial Interview Explanation (Model vs Browser Speech)**:
- Our LLM supports English, Hindi, and Hinglish text natively.
- Chrome's Web Speech API **does not have a dedicated `hinglish` locale**, so `static/call.js` maps `en -> en-IN`, `hi -> hi-IN`, and `hinglish -> en-IN` (`RECOGNITION_LANG_MAP`).
- Likewise, browser TTS quality depends on OS voices installed on that laptop; if no `hi-IN` voice is installed locally, `#voice-note` explains why playback skipped while the Devanagari reply renders cleanly in the transcript.

---

## Scenario E — Booking Safety (Two-Step Guardrails)

Run these 5 checks in order (use a fresh call with Phone blank or `+919900990099`, Language `en`):

1. **Propose Without Booking**:
   - **Type**: `"Please check if tomorrow at 15:00 is available for a demo."`
   - **Expected**: Agent offers `15:00` (*"I have ... available. Would you like me to confirm this slot for you?"*). DB `sessions.pending_slot` = `'YYYY-MM-DD 15:00'`; **`appointments` has NO new row**.
2. **Reject Proposed Slot**:
   - **Type**: `"No, please don't book that slot. Let's pick another time."`
   - **Expected**: `classify_booking_confirmation` returns `"reject"`, `validate_model_output` sets `clear_pending_slot = True`, replies with `format_booking_rejected_reply()` (*"No problem, I have not booked that slot..."*), and DB clears `sessions.pending_slot` back to `NULL`.
3. **Ambiguous Confirmation (New Slot)**:
   - First propose: `"Is tomorrow at 15:30 free?"` -> Agent sets `pending_slot` = `'YYYY-MM-DD 15:30'`.
   - Then reply ambiguously: `"Maybe, I need to check with my co-founder first."`
   - **Expected**: `classify_booking_confirmation` hits `AMBIGUOUS_TOKENS` (`"maybe"`, `"check"`), returns `"ambiguous"`, downgrades intent from `book_appointment` to `enquiry`, re-asks politely (`format_slot_proposal_reply`), and **still writes ZERO appointment rows**.
4. **Explicit Confirmation**:
   - **Type**: `"Yes, confirm that slot."`
   - **Expected**: `classify_booking_confirmation` returns `"confirm"`, slot matches `fresh_session["pending_slot"]`, `handle_booking()` commits `'confirmed'` row to `appointments`, sets `leads.status = 'booked'`, and replies `"Done! Your demo is booked for ..."`
5. **Attempt Already-Occupied Slot**:
   - **Type**: `"Actually, can you book tomorrow at 10:00 instead?"` *(remember: seeded `Dr. Ananya Sharma` holds `slots[0]` = `10:00` tomorrow!)*.
   - **Expected**: `slot_dt` is NOT in `available_slots` (`validate_model_output` triggers `format_alternative_slots_reply`), offering the next 2 open slots (`YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM`) and never overwriting `Dr. Ananya Sharma`'s booking. Even in a simultaneous race condition, SQLite `idx_active_slot` rejects the second `INSERT` cleanly inside `SAVEPOINT book_slot`.

---

## Scenario F — Resilience & Error Handling (Safe Demo Methods)

Do **not** corrupt your working `.env` or burn live quotas unnecessarily during the demo:

1. **Text Fallback When Mic Unavailable**:
   - Click the **microphone button** while idle or deny mic permission in Chrome -> `#error-banner` warns cleanly without crashing JS, and `#text-input` + **Send** continue using the exact same `sendTurn()` -> `POST /turn` endpoint.
2. **Primary Failure -> Gemini Fallback & Both Failing -> Scripted Fallback**:
   - Rather than breaking `.env`, run the pre-built offline mocked failover tests or targeted script:
     ```bat
     .venv\Scripts\python.exe -m pytest -q -k "secondary or fallback"
     ```
   - *(Optional only if explicitly asked to show live network failover, using existing `.env`)*: `.venv\Scripts\python.exe tools\test_failover.py` tests Case `(a)` invalid primary key -> Gemini failover (`[brain] provider=fallback`), Case `(b)` primary healthy, and Case `(c)` both failing -> scripted `fallback_reply()` on a temporary DB.
3. **Lead Data Preservation After Fallback**:
   - Point to `test_fallback_does_not_change_lead` in `tests/test_smoke.py` and `app/db.py` line `if not is_fallback: merge_lead_fields(...)`: fallback returns neutral placeholder score `20` for display/DB turn logging, **without overwriting** a caller's accumulated real `leads.score`, `sentiment`, or extracted details.
4. **Duplicate End-Call & Post-End Guard**:
   - Call `end_call` twice on the same session (via Swagger `/docs` or `test_end_call_twice`): returns the exact same saved JSON idempotently (`calls.session_id UNIQUE`) with zero duplicate LLM calls.
   - Sending `POST /turn` to an ended session returns HTTP `409 Conflict`, and `call.js` disables inputs immediately (`Session has already ended (409)`).

---

## Scenario G — Final Demo Checklists, Pacing Scripts & Troubleshooting

### Pre-Flight Manual Checklist
- [ ] `python tools/preflight.py` exits `0`
- [ ] `pytest -q` and `node tests/test_call_ui.js` pass
- [ ] `tools/reset_demo_db.py --rebuild` run cleanly before interview
- [ ] `.\run.bat` running on `http://127.0.0.1:8000`
- [ ] `/api/slots` checked in background tab so a free future slot is ready
- [ ] Chrome mic allowed and speaker volume tested

### Short Demo Script (5–7 Minutes)
1. **0:00–1:00**: Show `README.md` / Dashboard baseline (`3` leads, `1` booking). Explain business problem.
2. **1:00–3:30**: Run quick Scenario B call (`+919900112233`, ask Pro pricing + Mumbai security, share name/need/budget/timeline, propose + confirm tomorrow `11:30`, end call, inspect Summary Card + `/dashboard`).
3. **3:30–5:00**: Demonstrate returning caller (`+919900112233` shows welcome-back banner) + 1 Hinglish turn (`language="hinglish"`).
4. **5:00–6:30**: Show Booking Safety (`pending_slot` + double-booking check on `slots[0]`) and offline `pytest` resilience checks.

### Comprehensive Demo Script (15–20 Minutes)
Run Scenarios **A -> B (all 17 steps) -> C -> D (all 4 language inputs) -> E (all 5 safety cases) -> F (`pytest` resilience + `test_call_ui.js`)**, opening `/dashboard` and `/dashboard/calls/{id}` after Scenario B to inspect the full DB persistence and transcript.

### Troubleshooting Quick Reference

| Issue | Likely Cause | Immediate Fix |
| :--- | :--- | :--- |
| **Mic button disabled / `not-allowed`** | Chrome mic blocked or non-Chrome browser | Click address-bar lock icon -> **Allow Microphone** -> refresh; meanwhile use `#text-input` text fallback immediately. |
| **No audio playback + muted voice banner** | OS/Chrome lacks `en-IN` or `hi-IN` TTS voice | Continue seamlessly in text mode (`speakReply` already unlocks turn controls via `completeSpeech("no-voice")`). Mention OS voice packs are browser-level. |
| **`HTTP 401` / `Network error` from LLM** | Invalid/missing `LLM_API_KEY` or offline Wi-Fi | Check `.env` has valid key without quotes/spaces, or show mocked `pytest` suite while verifying connectivity. |
| **Agent returns generic apology (`fallback_reply`)** | Primary Groq `429` + secondary Gemini `503` overload | Built-in resilience activated safely (`is_fallback=True` protected lead score). Wait 5–10 seconds and resend turn. |
| **Port 8000 already in use** | Previous Uvicorn/Python process still running | Close old terminal or inspect with `netstat -ano \| findstr :8000` -> `taskkill /F /PID <pid>`. |
| **SQLite `database is locked` / reset refused** | `reset_demo_db.py` run while Uvicorn is active | Stop `run.bat` (`Ctrl+C`), run `reset_demo_db.py --rebuild`, restart `run.bat`. |
