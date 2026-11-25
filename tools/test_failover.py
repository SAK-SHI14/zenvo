import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import brain, db

SAMPLE_SLOTS = ["2026-11-26 10:00", "2026-11-26 10:30"]
SAMPLE_PROMPT = "Hi, what is the price of Zenvo Pro?"


# Clear brain's cached secondary client so env overrides take effect immediately.
def reset_secondary_cache() -> None:
    brain._SECONDARY_CLIENT_CACHE["key"] = None
    brain._SECONDARY_CLIENT_CACHE["base_url"] = None
    brain._SECONDARY_CLIENT_CACHE["client"] = None


# Extract which provider logged success from captured stdout without leaking anything else.
def detect_provider_used(stdout_text: str, is_fallback: bool) -> str:
    if "[brain] provider=primary" in stdout_text:
        return "primary"
    if "[brain] provider=fallback" in stdout_text:
        return "fallback"
    if is_fallback:
        return "none (polite_fallback)"
    return "unknown"


# Run Case (a): invalid primary key (401) must fail over to the real configured secondary provider.
def run_case_a(orig_client: OpenAI, orig_env: dict[str, str | None]) -> dict:
    has_fallback_env = all(
        (orig_env.get(k) or "").strip()
        for k in ("LLM_FALLBACK_API_KEY", "LLM_FALLBACK_BASE_URL", "LLM_FALLBACK_MODEL")
    )
    if not has_fallback_env:
        return {
            "case": "a) Primary 401 -> Gemini fallback",
            "status": "FAIL",
            "provider": "none",
            "latency": 0.0,
            "detail": "Missing LLM_FALLBACK_* env vars",
        }

    brain.LLM_CLIENT = OpenAI(
        api_key="invalid-primary-key",
        base_url=orig_env.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1",
        max_retries=0,
        timeout=8.0,
    )
    reset_secondary_cache()

    buf = io.StringIO()
    start = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        result = brain.generate_turn_response(
            lead={},
            recent_messages=[],
            available_slots=SAMPLE_SLOTS,
            user_text=SAMPLE_PROMPT,
            language="en",
        )
    latency = time.perf_counter() - start
    logs = buf.getvalue()

    provider = detect_provider_used(logs, result.get("fallback", True))
    ok = (
        result.get("fallback") is False
        and bool(str(result.get("reply", "")).strip())
        and "[brain] provider=fallback" in logs
    )
    return {
        "case": "a) Primary 401 -> Gemini fallback",
        "status": "PASS" if ok else "FAIL",
        "provider": provider,
        "latency": latency,
        "detail": "" if ok else "Expected fallback=False and [brain] provider=fallback",
    }


# Run Case (b): valid primary key succeeds and never invokes the secondary fallback provider.
def run_case_b(orig_client: OpenAI) -> dict:
    brain.LLM_CLIENT = orig_client
    reset_secondary_cache()

    secondary_called = False
    orig_secondary_fn = brain.call_secondary_for_turn

    def spy_secondary(chat_messages, language, deadline):
        nonlocal secondary_called
        secondary_called = True
        return orig_secondary_fn(chat_messages, language, deadline)

    brain.call_secondary_for_turn = spy_secondary
    buf = io.StringIO()
    start = time.perf_counter()
    try:
        with contextlib.redirect_stdout(buf):
            result = brain.generate_turn_response(
                lead={},
                recent_messages=[],
                available_slots=SAMPLE_SLOTS,
                user_text=SAMPLE_PROMPT,
                language="en",
            )
    finally:
        brain.call_secondary_for_turn = orig_secondary_fn
    latency = time.perf_counter() - start
    logs = buf.getvalue()

    provider = detect_provider_used(logs, result.get("fallback", True))
    ok = (
        result.get("fallback") is False
        and "[brain] provider=primary" in logs
        and not secondary_called
    )
    return {
        "case": "b) Primary OK -> no fallback",
        "status": "PASS" if ok else "FAIL",
        "provider": provider,
        "latency": latency,
        "detail": "" if ok else f"secondary_called={secondary_called}",
    }


# Run Case (c): both primary and fallback keys invalid -> fallback=True and lead score/sentiment preserved.
def run_case_c(orig_env: dict[str, str | None]) -> dict:
    brain.LLM_CLIENT = OpenAI(
        api_key="invalid-primary-key",
        base_url=orig_env.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1",
        max_retries=0,
        timeout=8.0,
    )
    os.environ["LLM_FALLBACK_API_KEY"] = "invalid-fallback-key"
    os.environ["LLM_FALLBACK_BASE_URL"] = orig_env.get("LLM_FALLBACK_BASE_URL") or "https://openrouter.ai/api/v1"
    os.environ["LLM_FALLBACK_MODEL"] = orig_env.get("LLM_FALLBACK_MODEL") or "google/gemini-2.0-flash-001"
    reset_secondary_cache()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = Path(tmp_dir) / "failover_test.db"
        os.environ["DB_PATH"] = str(tmp_db)
        db.init_db()

        lead, _ = db.find_or_create_lead("+919800009999")
        db.merge_lead_fields(lead["id"], score=82, sentiment="positive")
        session_id = "failover-test-session"
        db.create_session(session_id, lead["id"], "en")

        buf = io.StringIO()
        start = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            result = brain.generate_turn_response(
                lead=db.get_lead(lead["id"]) or {},
                recent_messages=[],
                available_slots=SAMPLE_SLOTS,
                user_text=SAMPLE_PROMPT,
                language="en",
            )
        latency = time.perf_counter() - start

        conn = db.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            db.save_turn(session_id, lead["id"], SAMPLE_PROMPT, "en", result, conn=conn)
            conn.commit()
        finally:
            conn.close()

        after_lead = db.get_lead(lead["id"])
        provider = detect_provider_used(buf.getvalue(), result.get("fallback", False))
        ok = (
            result.get("fallback") is True
            and after_lead is not None
            and after_lead["score"] == 82
            and after_lead["sentiment"] == "positive"
        )
        return {
            "case": "c) Both invalid -> polite fallback",
            "status": "PASS" if ok else "FAIL",
            "provider": provider,
            "latency": latency,
            "detail": "" if ok else "Expected fallback=True with score=82 and sentiment=positive",
        }


# Execute all 3 failover verification cases, restore env/client, and print the summary table.
def main() -> None:
    load_dotenv()
    env_keys = (
        "LLM_BASE_URL",
        "LLM_FALLBACK_API_KEY",
        "LLM_FALLBACK_BASE_URL",
        "LLM_FALLBACK_MODEL",
        "DB_PATH",
    )
    orig_env = {k: os.environ.get(k) for k in env_keys}
    orig_client = brain.LLM_CLIENT

    rows: list[dict] = []
    try:
        rows.append(run_case_a(orig_client, orig_env))
        time.sleep(2.0)
        rows.append(run_case_b(orig_client))
        time.sleep(1.0)
        rows.append(run_case_c(orig_env))
    finally:
        brain.LLM_CLIENT = orig_client
        for key, val in orig_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        reset_secondary_cache()

    print("\n=== FAILOVER TEST RESULTS ===")
    print(f"{'case':<36} | {'PASS/FAIL':<9} | {'provider used':<22} | latency")
    print("-" * 85)
    for row in rows:
        print(
            f"{row['case']:<36} | {row['status']:<9} | "
            f"{row['provider']:<22} | {row['latency']:.2f}s"
        )
        if row["status"] == "FAIL" and row["detail"]:
            print(f"  -> {row['detail']}")


if __name__ == "__main__":
    main()
