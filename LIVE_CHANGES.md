# Zenvo — Live Coding & Modification Exercises (`Practice Only`)

> **Important**: These are **practice exercises** to prepare you for live interviewer requests. Do **not** apply these edits permanently before your demo unless asked. Always keep `git status` / clean backups ready so you can revert instantly.

### Safety Classification Quick Reference

| Risk Level | Typical Scope | Server Restart Needed? | Automated Test Impact |
| :--- | :--- | :--- | :--- |
| **Safe Live Edits** | CSS variables, browser speech rate, KB wording/FAQs | CSS/JS: Browser refresh only (`Ctrl+Shift+R`); `kb/*.md`: Yes (restart Uvicorn so `KB_TEXT` reloads) | Low (verify `test_call_ui.js` for JS syntax) |
| **Moderate Edits** | Message length cap, qualification threshold, fallback copy, phone mask, new KPI | Yes (restart Uvicorn) | May break `pytest` assertions coupled to old limits/values; re-run `pytest -q` |
| **Higher-Risk / Careful Edits** | Gemini retry delay/count, adding new intent handlers, system prompt JSON schema | Yes (restart Uvicorn) | Yes—retry count/delay and schema tests in `tests/test_smoke.py` assert exact values; update tests if changing contracts |

---

## Exercise 1 — Change the UI Accent Colour *(Safe Live Edit)*
- **Interviewer Request**: *"Can you change the green brand accent colour across the call desk and dashboard to navy blue?"*
- **Locate**: `:root` in `static/call.css` (lines 13–15) and `static/dashboard.css` (lines 13–15).
- **Existing Code**:
```css
  --accent: #1B4D3E;
  --accent-hover: #143B2F;
  --accent-soft: #E5EDE9;
```
- **Minimal Modification**:
```css
  --accent: #1E3A8A;
  --accent-hover: #172554;
  --accent-soft: #DBEAFE;
```
- **Why It Works**: Both CSS files centralize buttons, focus outlines, score bars, badges, and microphone active states using CSS custom properties.
- **Verify**: Hard-refresh Chrome (`Ctrl+Shift+R`) on `/` and `/dashboard` (no server restart needed).
- **Revert**: Restore `#1B4D3E`, `#143B2F`, `#E5EDE9` (or `git checkout -- static/call.css static/dashboard.css`).

---

## Exercise 2 — Change the Page Background or Typography *(Safe Live Edit)*
- **Interviewer Request**: *"Change the cream page background to cool white and switch body typography."*
- **Locate**: `:root` in `static/call.css` (lines 7, 19–20) and `static/dashboard.css` (lines 7, 21–22).
- **Existing Code**:
```css
  --bg-page: #F6F2EA;
  --font-display: "Fraunces", Georgia, serif;
  --font-body: "IBM Plex Sans", "Noto Sans Devanagari", sans-serif;
```
- **Minimal Modification**:
```css
  --bg-page: #F8FAFC;
  --font-display: Georgia, serif;
  --font-body: system-ui, "Noto Sans Devanagari", sans-serif;
```
- **Why It Works**: Keep `"Noto Sans Devanagari"` in `--font-body` so Hindi (`hi`) text still renders cleanly without missing glyphs.
- **Verify**: Hard-refresh `/` and `/dashboard`. Test Hindi rendering visually.
- **Revert**: Restore original hex/font values.

---

## Exercise 3 — Change the Maximum Message Length *(Moderate — Restart + Re-test)*
- **Interviewer Request**: *"Increase the caller turn limit from 500 characters to 800 characters."*
- **Locate**:
  1. `app/main.py` -> `TurnRequest.validate_text` (lines 78–82)
  2. `templates/call.html` -> `#text-input` (lines 95–98)
  3. *(Note error message in `static/call.js` line 631)*
- **Existing Code (`app/main.py`)**:
```python
    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not (1 <= len(cleaned) <= 500):
            raise ValueError("text must be between 1 and 500 characters")
        return cleaned
```
- **Existing Code (`templates/call.html`)**:
```html
          <input
            id="text-input"
            type="text"
            maxlength="500"
            placeholder="Or type your message here..."
            disabled
          />
```
- **Minimal Modification**: Update `500` to `800` in both `app/main.py` (`validate_text` condition & message) and `templates/call.html` (`maxlength="800"`), plus `call.js` line 631 if helpful.
- **Why It Works**: Frontend `maxlength` stops overly long typing in the browser while Pydantic `TurnRequest` enforces the true security/token boundary on the API.
- **Verify**: Restart Uvicorn; test a 600-char turn via `/docs` or UI; run `pytest -q` (update any test asserting `501` chars fails if needed).
- **Revert**: Restore `500` in all edited files.

---

## Exercise 4 — Change Lead Qualification Threshold Consistently *(Moderate — Multi-File + Restart)*
- **Interviewer Request**: *"Raise the qualified/warm lead threshold from score 40 to 50."*
- **Locate**:
  1. `app/db.py` -> `compute_lead_status` (lines 173–180)
  2. `app/dashboard.py` -> `fetch_kpis` (line 66)
  3. `templates/dashboard.html` -> KPI card caption (line 42)
- **Existing Code (`app/db.py` / `app/dashboard.py` / `templates/dashboard.html`)**:
```python
# app/db.py
    if score >= 40:
        return "warm"
```
```sql
-- app/dashboard.py
SUM(CASE WHEN COALESCE(score, 0) >= 40 OR status IN ('warm', 'hot', 'booked') THEN 1 ELSE 0 END) AS qualified_leads
```
```html
<!-- templates/dashboard.html -->
<span class="kpi-note">Score 40+ or booked</span>
```
- **Minimal Modification**: Replace `40` with `50` in all three places so DB status labeling, SQL KPI aggregation, and dashboard help text match.
- **Why It Works**: Prevents UI caption drift vs backend qualification rules. Note that previously stored `'warm'` leads stay counted due to `OR status IN (...)`.
- **Verify**: Restart Uvicorn, reload `/dashboard` (seeded `Dr. Harpreet Gill` has score `48`), and run `.venv\Scripts\python.exe -m pytest -q`.
- **Revert**: Restore `40` across all three files.

---

## Exercise 5 — Add or Modify a Product FAQ in the Knowledge Base *(Safe Live Edit — Restart Needed)*
- **Interviewer Request**: *"Add a new FAQ stating that clinic data exports are available anytime in CSV format."*
- **Locate**: `kb/faqs.md` (lines 18–20).
- **Existing Code**:
```markdown
**Q: Does Zenvo integrate with our existing clinic software?**
A: Pro and Enterprise plans support webhook and API integrations, plus native sync with Google Calendar and major Indian clinic HMS tools.
```
- **Minimal Modification** (append to `kb/faqs.md`):
```markdown

**Q: Can our clinic export appointment and confirmation logs?**
A: Yes. Clinics can download daily appointment logs anytime in CSV format from the Zenvo portal.
```
- **Why It Works**: `app/brain.py` calls `load_knowledge_base()` once at module import into `KB_TEXT`, which is injected directly into `build_system_prompt`.
- **Verify**: **Restart Uvicorn** (mandatory because `KB_TEXT` is cached at startup!), start a call, and ask *"Can we export our clinic appointment logs?"*.
- **Revert**: Remove the appended Q&A from `kb/faqs.md` and restart.

---

## Exercise 6 — Change Basic or Pro Pricing Consistently Across KB *(Safe/Moderate — Restart Needed)*
- **Interviewer Request**: *"Update the Basic plan price from Rs 999/month to Rs 1,199/month."*
- **Locate**: `kb/pricing.md` (lines 5–10); optionally check `app/seed.py` historical sample text (lines 47, 54, 64) if aligning demo seed records.
- **Existing Code (`kb/pricing.md`)**:
```markdown
## Basic Plan - Rs 999 / month
- Up to 500 appointment reminders per month
```
- **Minimal Modification (`kb/pricing.md`)**:
```markdown
## Basic Plan - Rs 1,199 / month
- Up to 500 appointment reminders per month
```
- **Why It Works**: `kb/pricing.md` is the single KB source of truth for pricing (`kb/faqs.md` mentions only `Rs 1` overage, not plan prices). Because `KB_TEXT` loads at startup, Uvicorn must restart.
- **Verify**: Restart Uvicorn, start a call, ask *"What does the Basic plan cost?"*.
- **Revert**: Restore `Rs 999 / month` in `kb/pricing.md` and restart.

---

## Exercise 7 — Change the Scripted Fallback Reply *(Safe/Moderate — Restart + Re-test)*
- **Interviewer Request**: *"Update the English fallback reply to include a callback phone number."*
- **Locate**: `app/brain.py` -> `fallback_reply(language)` (lines 217–224).
- **Existing Code**:
```python
    replies = {
        "en": "Sorry, I had a brief connection issue. Let me have a human specialist follow up with you shortly.",
        "hi": "Maaf kijiye, thodi network samasya aa gayi hai. Hamare team member jaldi aapse sampark karenge.",
        "hinglish": "Sorry, thoda network issue aa gaya hai. Hamare specialist aapko jaldi follow up karenge.",
    }
```
- **Minimal Modification**: Edit `"en"` string while keeping `fallback=True`, `lead_score=20`, and `VALID_LANGUAGES` keys untouched.
- **Why It Works**: Keeps `"fallback": True` intact so `save_turn()` continues protecting lead scores during outages.
- **Verify**: Restart Uvicorn and run `.venv\Scripts\python.exe -m pytest -q`.
- **Revert**: Restore original string in `app/brain.py`.

---

## Exercise 8 — Change the Gemini Retry Interval *(Higher Risk — Coupled to Tests)*
- **Interviewer Request**: *"Reduce the wait between Gemini retries from 1.0s to 0.5s."*
- **Locate**: `app/brain.py` (line 24).
- **Existing Code**:
```python
SECONDARY_RETRY_DELAY_SECONDS = 1.0
```
- **Minimal Modification**:
```python
SECONDARY_RETRY_DELAY_SECONDS = 0.5
```
- **Why It Works**: `call_secondary_with_5xx_retry()` references `SECONDARY_RETRY_DELAY_SECONDS` both to sleep and to check `deadline - time.monotonic() <= SECONDARY_RETRY_DELAY_SECONDS + 0.2`.
- **Verify**: Restart Uvicorn and run `.venv\Scripts\python.exe -m pytest -q` (note existing mocked retry tests assert `1.0s` `sleep`, so those test assertions must be updated if permanently changed!).
- **Revert**: Restore `1.0`.

---

## Exercise 9 — Change the Maximum Number of Retries *(Higher Risk — Coupled to Tests)*
- **Interviewer Request**: *"Allow 1 retry (2 total attempts) on Gemini instead of 2 retries (3 total attempts)."*
- **Locate**: `app/brain.py` (line 25).
- **Existing Code**:
```python
SECONDARY_MAX_ATTEMPTS = 3
```
- **Minimal Modification**:
```python
SECONDARY_MAX_ATTEMPTS = 2
```
- **Why It Works**: Controls `for attempt in range(1, SECONDARY_MAX_ATTEMPTS + 1):` inside `call_secondary_with_5xx_retry()`.
- **Verify**: Run `.venv\Scripts\python.exe -m pytest -q` (existing tests expect 3 attempts on repeated `503`s—be transparent with the interviewer that unit test expectations update alongside this config constant).
- **Revert**: Restore `3`.

---

## Exercise 10 — Change the Phone Masking Format *(Moderate — Restart + Re-test)*
- **Interviewer Request**: *"Show only the last 2 digits instead of the last 4 digits on the CRM dashboard."*
- **Locate**: `app/dashboard.py` -> `mask_phone` (lines 11–19).
- **Existing Code**:
```python
    if len(cleaned) <= 4:
        return "****"
    prefix = cleaned[:3] if cleaned.startswith("+") and len(cleaned) > 7 else ""
    masked_len = max(4, len(cleaned) - len(prefix) - 4)
    return f"{prefix}{'*' * masked_len}{cleaned[-4:]}"
```
- **Minimal Modification**:
```python
    if len(cleaned) <= 2:
        return "**"
    prefix = cleaned[:3] if cleaned.startswith("+") and len(cleaned) > 7 else ""
    masked_len = max(4, len(cleaned) - len(prefix) - 2)
    return f"{prefix}{'*' * masked_len}{cleaned[-2:]}"
```
- **Why It Works**: Both `/dashboard` (`fetch_leads`, `fetch_appointments`, `fetch_recent_calls`) and `/dashboard/calls/{id}` funnel through `mask_phone()`, so changing it once updates every view consistently.
- **Verify**: Restart Uvicorn, check `/dashboard` + `/dashboard/calls/1`, run `pytest -q` (note dashboard tests assert `******NNNN` masking, so re-check assertions!).
- **Revert**: Restore `-4` slice logic.

---

## Exercise 11 — Add a Simple Dashboard KPI *(Moderate — Multi-File + Restart)*
- **Interviewer Request**: *"Add a 'Hot Leads' KPI card to the dashboard."*
- **Locate**:
  1. `app/dashboard.py` -> `fetch_kpis` (lines 60–94)
  2. `templates/dashboard.html` -> `.kpi-grid` (lines 28–59)
  3. `static/dashboard.css` -> `.kpi-grid` (`grid-template-columns: repeat(6, minmax(0, 1fr));`, line 114)
- **Existing Code (`app/dashboard.py`)**:
```python
    return {
        "total_leads": total_leads,
        "completed_calls": completed_calls,
        "qualified_leads": qualified_leads,
        "appointments_booked": appointments_booked,
        "avg_score": avg_score,
        "conversion_rate": conversion_rate,
        "conversion_note": f"{appointments_booked} booked / {total_leads} total leads",
    }
```
- **Minimal Modification**:
  1. Query `SUM(CASE WHEN score >= 70 OR status = 'hot' THEN 1 ELSE 0 END) AS hot_leads` inside `fetch_kpis` and add `"hot_leads": int(lead_row["hot_leads"] or 0)` to the returned dict.
  2. Add a `<div class="kpi-card">` block inside `templates/dashboard.html` displaying `{{ kpis.hot_leads }}`.
  3. Change `repeat(6, minmax(0, 1fr))` -> `repeat(7, minmax(0, 1fr))` (or `repeat(auto-fit, minmax(150px, 1fr))`) in `static/dashboard.css` so the 7th card sits cleanly in the row.
- **Verify**: Restart Uvicorn, reload `/dashboard` (`Rajesh Verma` has `hot`), verify mobile wrap + `pytest -q`.
- **Revert**: Remove the SQL column/card and restore `repeat(6, ...)`.

---

## Exercise 12 — Conceptual: Add Another Deterministic Intent Handler *(Architecture Explanation)*
- **Interviewer Request**: *"How would you add a new `request_callback` intent that logs a callback flag cleanly?"*
- **Files & Functions Involved**:
  1. `app/brain.py`: Add `"request_callback"` to `VALID_INTENTS` (line 16) and describe it under `INTENT GUIDE:` in `build_system_prompt()` (lines 108–114).
  2. `app/handlers.py`: Write `handle_callback(context: dict) -> dict` using `context["lead_id"]`, `context["conn"]`, and a localized confirmation reply.
  3. `app/router.py`: Register `"request_callback": handle_callback` inside `INTENT_HANDLERS` (lines 4–6).
  4. `app/main.py`: Add any server-side validation rules inside `validate_model_output()` before `router.dispatch_intent()` runs inside `BEGIN IMMEDIATE`.
- **Why It Works**: `app/router.py` uses a dict dispatch table (`INTENT_HANDLERS.get(intent)`), so adding a deterministic capability takes one handler function plus one dict entry without rewriting `handle_turn()`.

---

## Exercise 13 — Conceptual: Modify a System Prompt Safely *(Architecture Explanation)*
- **Interviewer Request**: *"How would you make Zenvo sound slightly warmer or add a new extraction rule to the prompt?"*
- **Files & Functions Involved**:
  - Per-turn sales prompt: `app/brain.py` -> `build_system_prompt(lead, available_slots, language)` (lines 82–118).
  - Post-call analyst prompt: `app/summary.py` -> `call_analyst_llm(transcript)` (lines 111–118).
- **Safety Rule to Emphasize**: Never remove or reorder the JSON schema instructions (`"reply"`, `"language"`, `"intent"`, `"sentiment"`, `"lead_score"`, `"extracted"`, `"slot_datetime"` in `brain.py`, or `"summary"`, `"overall_sentiment"`, `"final_score"` in `summary.py`) because `normalize_llm_result()` and `is_valid_summary_payload()` strictly enforce those exact keys and trigger fallbacks if missing. Always restart Uvicorn and test with live/mocked turns afterward.

---

## Exercise 14 — Change the Browser Speech Rate *(Safe Live Edit)*
- **Interviewer Request**: *"Zenvo speaks a bit fast in English—slow the voice down slightly."*
- **Locate**: `static/call.js` -> `speakReply(text, lang)` (lines 474–477).
- **Existing Code**:
```javascript
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = matchedVoice;
  utterance.lang = matchedVoice.lang || RECOGNITION_LANG_MAP[lang] || "en-IN";
  utterance.rate = 1.0;
```
- **Minimal Modification**:
```javascript
  utterance.rate = 0.92;
```
- **Why It Works**: `SpeechSynthesisUtterance.rate` (`1.0` normal, `0.9` slightly slower, `2.0` double speed) adjusts browser TTS pacing immediately while keeping the watchdog timer in `speakReply` (`Math.max(8000, Math.min(20000, ...))`) functioning safely. *(Note: if slowing below `0.8`, lengthen `watchdogMs` slightly so longer sentences aren't timed out early.)*
- **Verify**: Hard-refresh Chrome (`Ctrl+Shift+R`), start a call, listen to greeting, and run `node tests\test_call_ui.js`.
- **Revert**: Restore `utterance.rate = 1.0;`.
