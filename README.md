# Zenvo — Autonomous AI Sales Voice Agent

Zenvo is a multilingual autonomous AI sales voice agent and CRM analytics workspace built for clinics and healthcare practices. It conducts interactive inbound sales calls in **English (`en`)**, **Hindi (`hi`)**, and **Hinglish (`hinglish`)**, grounds answers in local Markdown product documentation (`kb/*.md`), tracks live sentiment and lead qualification scores, confirms demo appointments via a guarded two-step flow, and logs structured post-call summaries to a SQLite-backed CRM dashboard.

---

## Features

- **Multilingual Voice & Text Console (`/`)**: Browser voice interface powered by Web Speech API (`en-IN` for English/Hinglish, `hi-IN` for Hindi) with automatic text-input fallback, race-guarded microphone teardown, and live transcript updates.
- **Grounded Knowledge Base (`kb/*.md`)**: System prompts incorporate all six product knowledge files covering pricing, features, FAQs, refund policy, onboarding, and security.
- **Resilient Dual-Provider LLM Pipeline**: Primary inference on **Groq** (`openai/gpt-oss-120b`) with failover to **Google Gemini** (`gemini-3.5-flash-lite`) using bounded `5xx` retries (`500/502/503/504`) inside strict deadlines (`10.0s` turn, `20.0s` summary), falling back cleanly to scripted multilingual templates (`app/handlers.py`).
- **Two-Step Slot Booking**: Proposes available 30-minute demo slots (`10:00–16:30` across the next 3 days) into `sessions.pending_slot` first, and commits an `appointments` row only after explicit confirmation on a subsequent turn.
- **Server-Rendered CRM Dashboard (`/dashboard`)**: Displays live KPIs (total leads, completed calls, qualified leads, booked appointments, average lead score, conversion rate), masked phone numbers (`******NNNN`), recent lead records, and detailed call views (`/dashboard/calls/{call_id}`).
- **Single-Command Windows Launcher & Portable Packager**: Includes `run.bat` for zero-friction setup/startup and `tools/package_project.py` for clean ZIP distribution.

---

## Tech Stack

- **Backend**: Python 3.11+, FastAPI (`0.165.0`), Uvicorn (`0.54.0`), Jinja2 (`3.1.6`), HTTPX (`0.28.1`)
- **Database**: SQLite (`zenvo.db` by default) with WAL journaling and automatic schema initialization (`app/db.py`)
- **LLM Client**: OpenAI-compatible Python SDK (`openai 3.59.0`) + `python-dotenv` (`1.2.4`)
- **Frontend**: Plain HTML5, CSS3, and Vanilla JavaScript (Web Speech API) — no React, Tailwind, or Node build step
- **Testing**: `pytest` (Python backend/integration suite) and Node.js (`tests/test_call_ui.js` UI regression checks)

---

## Repository Structure

```text
Project-Sakshi-Placement/
├── app/
│   ├── brain.py          # System prompt builder, schema validation, Groq/Gemini failover
│   ├── dashboard.py      # Server-rendered /dashboard and /dashboard/calls/{call_id} routes
│   ├── db.py             # SQLite connection, schema init/migration, CRUD & slot helpers
│   ├── handlers.py       # Scripted fallback replies and slot/confirmation templates
│   ├── main.py           # FastAPI app, /, /health, /api/slots, /start_call, /turn, /end_call
│   ├── router.py         # Deterministic intent dispatch registry
│   ├── seed.py           # Demo leads, calls, and appointment seed data
│   └── summary.py        # Deterministic heuristics + LLM-assisted post-call summary
├── kb/                   # Grounded product docs (pricing, features, faqs, refund, onboarding, security)
├── static/               # call.css, call.js, dashboard.css
├── templates/            # call.html, dashboard.html, call_detail.html
├── tests/                # Pytest test suite and tests/test_call_ui.js
├── tools/
│   ├── package_project.py # Allowlisted ZIP packager for portable transfer
│   ├── preflight.py       # Read-only pre-demo configuration and health checker
│   ├── reset_demo_db.py   # Backs up DB and resets/re-seeds demo records
│   ├── real_run_check.py  # Live end-to-end provider check (uses API quota)
│   ├── test_one_model.py  # Single-model benchmark and --diag tool (uses API quota)
│   └── test_failover.py   # Failover test utility (uses API quota)
├── .env.example          # Environment variable template (no real keys)
├── .gitignore            # Excludes .env, .venv, *.db, caches, logs, and *.zip
├── CODE_WALKTHROUGH.md   # Execution-flow and frontend walkthrough for Python devs
├── DEMO_GUIDE.md         # Step-by-step live demo script and troubleshooting
├── INTERVIEW_PREP.md     # Q&A prep across product, backend, LLM, voice & scale
├── LIVE_CHANGES.md       # Practice live code-modification exercises
├── README.md             # Project documentation and setup guide
├── REPORT.md             # Concise engineering summary report
├── architecture.md       # System diagrams, 100k-user scale math, security & cost analysis
├── requirements.txt      # Core runtime dependencies and optional pytest
└── run.bat               # Windows one-command setup and server launcher
```

---

## Prerequisites

- **Windows 10/11** (for `run.bat`; Python commands also work cross-platform)
- **Python 3.11+** installed with `python` (or `py -3`) available on `PATH`
- **Groq API key** (`LLM_API_KEY`) for primary inference; optional **Gemini API key** (`LLM_FALLBACK_API_KEY`) for fallback
- **Optional**: **Google Chrome / Microsoft Edge** for Web Speech API voice testing (text mode works in any browser)
- **Optional**: **Node.js 18+** only if running `node tests/test_call_ui.js`

---

## First-Time Setup & Environment Variables

1. Copy `.env.example` to `.env` (or run `.\run.bat` once to auto-create `.env`).
2. Add your API key(s) in `.env`:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `LLM_BASE_URL` | Primary OpenAI-compatible API endpoint | `https://api.groq.com/openai/v1` |
| `LLM_API_KEY` | Primary provider API key (**required for live LLM calls**) | `your-groq-api-key-here` |
| `LLM_MODEL` | Primary chat model identifier | `openai/gpt-oss-120b` |
| `LLM_FALLBACK_BASE_URL` | Optional secondary endpoint | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_FALLBACK_API_KEY` | Optional secondary provider API key | `your-gemini-api-key-here` |
| `LLM_FALLBACK_MODEL` | Optional fallback model identifier | `gemini-3.5-flash-lite` |
| `DB_PATH` | SQLite database file path | `zenvo.db` |

---

## Running the Application

### Option A: Single-Command Windows Launcher (Recommended)

From the repository root (or by double-clicking `run.bat` in File Explorer):

```bat
.\run.bat
```

What `run.bat` does:
1. Resolves relative paths safely (including folder names with spaces).
2. Verifies Python availability (`python` or `py -3`).
3. Creates `.venv` and installs `requirements.txt` on first run only.
4. Creates `.env` from `.env.example` if missing and pauses with clear instructions so you can add `LLM_API_KEY`.
5. Initializes the SQLite database (`init_db()`) without erasing existing call/lead history.
6. Starts the single FastAPI server serving both the Voice Console (`/`), CRM Dashboard (`/dashboard`), and API endpoints at `http://127.0.0.1:8000`.

### Option B: Manual Startup

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
rem Edit .env and set LLM_API_KEY
python -c "from app.db import init_db; init_db()"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## Application URLs & API Endpoints

Once running, open:
- **Voice Call Console**: `http://127.0.0.1:8000/`
- **CRM Analytics Dashboard**: `http://127.0.0.1:8000/dashboard`
- **Call Detail View**: `http://127.0.0.1:8000/dashboard/calls/{call_id}`
- **Interactive API Docs**: `http://127.0.0.1:8000/docs`

| Method & Path | Purpose | Key Payload / Output |
| :--- | :--- | :--- |
| `GET /` | Serve Voice Call Console UI | `templates/call.html` |
| `GET /dashboard` | Serve CRM KPIs & Leads table | `templates/dashboard.html` |
| `GET /dashboard/calls/{call_id}` | Inspect single call transcript & timeline | `templates/call_detail.html` (`404` if missing) |
| `GET /health` | Liveness check | `{"status": "ok"}` |
| `GET /api/slots` | List open 30-min demo slots (next 3 days, 10:00–16:30) | `{"slots": ["YYYY-MM-DD HH:MM", ...]}` |
| `POST /start_call` | Create session & lead record (detects returning callers) | `{phone?, language}` -> `{session_id, lead_id, greeting, language, returning_lead}` |
| `POST /turn` | Run dialogue turn (`1–500` chars; `409` ended; `429` at 60 turns) | `{session_id, text, language?}` -> `{reply, sentiment, lead_score, intent, language}` |
| `POST /end_call` | Close session, generate summary & log call | `{session_id}` -> `{summary, objections, buying_signals, next_best_action, overall_sentiment, final_score, sentiment_timeline}` |

---

## Quick Demo Walkthrough (7 Steps)

Follow this sequence in the browser (`http://127.0.0.1:8000/`) using either Voice mode or the built-in Text box:

1. **Start a Call**: Enter an optional phone number (e.g., `9876543210`) or leave it blank, choose `en`, `hi`, or `hinglish`, and click **Start Call**. Zenvo greets the caller via `POST /start_call`.
2. **Ask About Pricing (`kb/pricing.md`)**: Send `"What are the prices for Basic and Pro plans?"` — Zenvo answers with **Rs 999/month** (Basic: 500 reminders, 1 clinic, 2 doctors) and **Rs 2,499/month** (Pro: 2,500 reminders, SMS/WhatsApp/AI voice, 3 clinics, 10 doctors), noting **+18% GST** and **2 months free** on annual billing.
3. **Ask About Trial, Security & Refunds (`kb/faqs.md`, `kb/refund_policy.md`, `kb/security.md`)**: Send `"Do you have a free trial and refund guarantee? Where is data hosted?"` — Zenvo cites the **14-day free trial** (up to 50 free reminders, no card required), **14-day 100% money-back guarantee** (`billing@zenvo.in`, 5–7 business days), and **TLS 1.3 / AES-256** hosting in Indian cloud data centers (Mumbai region).
4. **Test Multilingual Switching**: Send `"Hinglish mein batao, extra reminder kitne ka padta hai?"` — Zenvo replies in Hinglish (`Rs 1` per extra reminder) and switches the active session language to `hinglish`.
5. **Propose a Demo Slot (Turn 1 of 2-Step Booking)**: Send `"Can we schedule a demo tomorrow at 11:00?"` (or pick a free slot from `GET /api/slots`). Zenvo proposes the slot (`sessions.pending_slot`) and asks you to confirm — no appointment is committed yet.
6. **Confirm the Slot (Turn 2 of 2-Step Booking)**: Reply `"Yes, confirm that slot"` (or `"Haan ji, book kar do"`). Zenvo confirms and writes a row into SQLite `appointments`.
7. **End Call & Inspect CRM Dashboard**: Click **End Call** to view the call summary, objections, buying signals, next best action, and sentiment timeline (`POST /end_call`). Click the link to **`/dashboard`** to verify updated KPIs, masked phone display, qualification badge, and click into `/dashboard/calls/{call_id}`.

---

## Testing

Run the automated test suites from `.venv`:

```bat
.venv\Scripts\python.exe -m pytest -q
node tests\test_call_ui.js
```

> **Note**: Scripts in `tools/test_one_model.py`, `tools/real_run_check.py`, and `tools/test_failover.py` call live external LLM APIs and consume quota; keep `pytest` (mocked LLM calls) and `node tests\test_call_ui.js` as your routine offline verification suite.

---

## Resetting the Demo Database

Stop the running Uvicorn server first (`Ctrl+C`), then rebuild demo seed data inside `.venv`:

```bat
.venv\Scripts\python.exe tools\reset_demo_db.py --rebuild
```

> **Warning**: Resetting the database backs up the current `.db` file to `<db>.bak.<timestamp>`, deletes all runtime rows from `appointments`, `calls`, `messages`, `sessions`, and `leads`, and re-inserts demo seed records (`app/seed.py`). Any recorded calls from manual testing will be cleared from the active database.

---

## Packaging & Transferring to Another Windows Laptop

Create a clean, credential-free ZIP archive outside the repository folder:

```bat
python tools\package_project.py
```

This generates `..\Project-Sakshi-Placement-submission-<timestamp>.zip` using a strict allowlist (excluding `.env`, `.venv/`, `*.db`, backups, caches, and logs). Note that `.gitignore` ignores `*.zip` so archives are never accidentally committed to Git.

### On the New Laptop (After Extracting the ZIP)

1. Extract the ZIP folder to a local directory (for example, `C:\Users\<Name>\Projects\Zenvo`).
2. Open the extracted folder and run `.\run.bat` (or double-click `run.bat`).
3. On first launch, `run.bat` creates `.venv`, installs dependencies, generates `.env` from `.env.example`, and asks you to add your API key.
4. Open `.env`, set `LLM_API_KEY` (and optionally `LLM_FALLBACK_API_KEY`), save, and run `.\run.bat` again.
5. Open `http://127.0.0.1:8000/` in Chrome or Edge.

---

## Architecture & Report

- Detailed system diagrams, 100,000-user scaling calculations, security controls, and Built vs. Designed matrix: [`architecture.md`](architecture.md)
- Executive engineering summary (~500 words): [`REPORT.md`](REPORT.md)

---

## Interview and Demo Resources

Before presenting or reviewing the codebase, run the read-only preflight check:

```bat
python tools/preflight.py
```

Read the preparation documents in this recommended order:

1. [`CODE_WALKTHROUGH.md`](CODE_WALKTHROUGH.md) — Understand execution order, request flow, database transactions, and HTML/CSS/JS basics for Python developers.
2. [`DEMO_GUIDE.md`](DEMO_GUIDE.md) — Follow the step-by-step live demonstration scenarios (including pre-demo setup, sales call flow, booking safety, and troubleshooting).
3. [`INTERVIEW_PREP.md`](INTERVIEW_PREP.md) — Review realistic technical, business, LLM, and scalability interview questions and grounded answers.
4. [`LIVE_CHANGES.md`](LIVE_CHANGES.md) — Practice safe live code modifications, verification, and rollback steps.

---

## Known Limitations

- **Synchronous Request Handling**: `app/main.py` invokes synchronous OpenAI/HTTPX clients bounded by explicit deadlines (`10.0s` turn, `20.0s` summary), limiting single-process concurrency compared to async workers and connection pooling.
- **Browser Speech Hardware Dependency**: Voice input/output relies on browser `SpeechRecognition` / `SpeechSynthesis` availability (best supported in Chrome/Edge); unsupported browsers automatically fall back to text mode. Physical microphone/speaker hardware was not tested in headless CI.
- **Local Single-Node MVP Scope**: There is no authentication/RBAC on `/` or `/dashboard`, no external telephony (SIP/Twilio/Exotel) webhook adapter, no external CRM sync, and knowledge retrieval uses full `/kb/*.md` prompt inclusion rather than vector RAG.
