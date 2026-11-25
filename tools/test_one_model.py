import argparse
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import (
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    RateLimitError,
)

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.brain import build_chat_messages, build_system_prompt, safe_parse_json

REQUIRED_KEYS = (
    "reply",
    "language",
    "intent",
    "sentiment",
    "lead_score",
    "extracted",
    "slot_datetime",
)
SAMPLE_SLOTS = ["2026-11-26 10:00", "2026-11-26 10:30"]
TEST_CASES = [
    ("en", "Hi, what are your prices?"),
    ("hi", "मेरा छोटा क्लिनिक है, मुझे appointment reminders चाहिए। बजट लगभग 2000 है।"),
    ("hinglish", "Refund policy kya hai? Agar pasand na aaye toh paise wapas milenge?"),
]
RESULTS_PATH = Path(__file__).resolve().parent / "test_one_model_results.md"


# Parse CLI flags for target provider, model, run count, reasoning effort, and delay.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test one OpenAI-compatible LLM on Zenvo turns.")
    parser.add_argument("--base-url", default=None, help="Override LLM base URL")
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="Env var name holding the API key (default: LLM_API_KEY, or LLM_FALLBACK_API_KEY with --fallback)",
    )
    parser.add_argument("--fallback", action="store_true", help="Test secondary fallback provider from .env")
    parser.add_argument(
        "--diag",
        action="store_true",
        help="Run A/B/C/D diagnostics (5x each, 2s gap) using --fallback settings",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per test case (default: 3)")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
        default="low",
        help="Reasoning effort parameter for reasoning models (default: low)",
    )
    parser.add_argument("--no-json-mode", action="store_true", help="Disable response_format json_object")
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Seconds to wait between requests (default: 5.0)",
    )
    return parser.parse_args()


# Build an OpenAI-compatible client using CLI > shell env > .env precedence without logging keys.
def create_client(args: argparse.Namespace) -> tuple[OpenAI, str, str]:
    load_dotenv(override=False)

    if args.fallback or args.diag:
        key_env = args.api_key_env or "LLM_FALLBACK_API_KEY"
        base_url = (args.base_url or os.getenv("LLM_FALLBACK_BASE_URL", "")).strip()
        model = (args.model or os.getenv("LLM_FALLBACK_MODEL", "")).strip()
    else:
        key_env = args.api_key_env or "LLM_API_KEY"
        base_url = (args.base_url or os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")).strip()
        model = (args.model or os.getenv("LLM_MODEL", "openai/gpt-4o-mini")).strip()

    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        print(f"Error: {key_env} is missing in environment/.env")
        sys.exit(1)
    if not base_url:
        print("Error: base URL is missing")
        sys.exit(1)
    if not model:
        print("Error: model name is missing")
        sys.exit(1)

    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=15.0)
    return client, model, base_url


# Classify OpenAI/HTTP exceptions into short readable tags for the summary report.
def classify_error(exc: Exception) -> str:
    if isinstance(exc, RateLimitError):
        return "429"
    if isinstance(exc, APITimeoutError) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, AuthenticationError):
        return "auth"
    if isinstance(exc, NotFoundError):
        return "404"
    return type(exc).__name__


# Extract an optional separate reasoning string from a chat completion message if present.
def get_reasoning_text(message: object) -> str | None:
    for attr in ("reasoning", "reasoning_content"):
        val = getattr(message, attr, None)
        if val:
            return str(val)
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning", "reasoning_content"):
            if extra.get(key):
                return str(extra[key])
    return None


# Call the model once, sending reasoning_effort only for gpt-oss/gemini and stopping on Gemini param errors.
def call_model(
    client: OpenAI,
    model: str,
    messages: list[dict],
    effort: str,
    use_json_mode: bool,
) -> tuple[str, float, str | None]:
    model_lower = model.lower()
    is_gemini = "gemini" in model_lower
    send_effort = ("gpt-oss" in model_lower) or is_gemini
    send_json_mode = use_json_mode
    send_max_completion = not is_gemini

    start = time.perf_counter()
    while True:
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.3,
        }
        if send_max_completion:
            kwargs["max_completion_tokens"] = 1200
        else:
            kwargs["max_tokens"] = 1200
        if send_effort:
            kwargs["extra_body"] = {"reasoning_effort": effort}
        if send_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**kwargs)
            latency = time.perf_counter() - start
            message = response.choices[0].message
            reasoning_text = get_reasoning_text(message)
            content = message.content or ""
            return content, latency, reasoning_text
        except BadRequestError as exc:
            if is_gemini:
                print(f"Gemini rejected parameter: {exc}")
                sys.exit(1)
            err_msg = str(exc).lower()
            if send_json_mode and ("response_format" in err_msg or "json_object" in err_msg):
                send_json_mode = False
                print("no json mode")
                continue
            if send_effort and ("reasoning_effort" in err_msg or "reasoning" in err_msg or "extra_body" in err_msg):
                send_effort = False
                print("Warning: provider rejected reasoning_effort; retrying without it.")
                continue
            if send_max_completion and "max_completion_tokens" in err_msg:
                send_max_completion = False
                continue
            if send_effort:
                send_effort = False
                print("Warning: provider rejected reasoning_effort; retrying without it.")
                continue
            if send_json_mode:
                send_json_mode = False
                print("no json mode")
                continue
            if send_max_completion:
                send_max_completion = False
                continue
            raise


# Check whether the parsed JSON contains all required keys and a valid integer lead_score 0-100.
def check_schema_ok(parsed: dict | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    if not all(key in parsed for key in REQUIRED_KEYS):
        return False
    score = parsed.get("lead_score")
    if isinstance(score, bool) or not isinstance(score, int):
        return False
    return 0 <= score <= 100


# Verify the reply script matches the expected language (Devanagari or Latin).
def check_language_ok(lang: str, reply: str) -> bool:
    if not reply:
        return False
    has_devanagari = bool(re.search(r"[\u0900-\u097F]", reply))
    has_latin = bool(re.search(r"[A-Za-z]", reply))

    if lang == "hi":
        return has_devanagari
    if lang in ("en", "hinglish"):
        return has_latin and not has_devanagari
    return False


# Verify the reply is concise (at most 2 sentences) with no markdown syntax or emojis.
def check_style_ok(reply: str) -> bool:
    if not reply:
        return False
    if re.search(r"[*`#_]", reply):
        return False
    if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", reply):
        return False
    sentences = [s for s in re.split(r"[.!?।]+(?:\s+|$)", reply.strip()) if s.strip()]
    return len(sentences) <= 2


# Check whether the English pricing reply mentions Zenvo's Basic or Pro prices from the KB.
def check_kb_ok(lang: str, reply: str) -> bool | None:
    if lang != "en":
        return None
    return any(price in reply for price in ("999", "2,499", "2499"))


# Execute one test turn and return structured metrics plus a single-line summary string.
def run_single_case(
    client: OpenAI,
    model: str,
    lang: str,
    user_text: str,
    run_idx: int,
    effort: str,
    use_json_mode: bool,
) -> dict:
    # Reuses app.brain.build_system_prompt and KB_TEXT directly with empty lead and sample slots.
    system_prompt = build_system_prompt(lead={}, available_slots=SAMPLE_SLOTS, language=lang)
    messages = build_chat_messages(system_prompt, recent_messages=[], user_text=user_text)

    try:
        content, latency, reasoning_text = call_model(client, model, messages, effort, use_json_mode)
    except Exception as exc:
        err_tag = classify_error(exc)
        line = (
            f"[{lang} run {run_idx}] latency=0.00s valid_json=False schema_ok=False "
            f"language_ok=False style_ok=False kb_ok=n/a error={err_tag}"
        )
        print(line)
        return {
            "lang": lang,
            "latency": None,
            "valid_json": False,
            "schema_ok": False,
            "language_ok": False,
            "style_ok": False,
            "kb_ok": None,
            "reply": "",
            "error": err_tag,
            "line": line,
        }

    if reasoning_text:
        print(f"  [{lang} run {run_idx}] reasoning_length={len(reasoning_text)}")

    if not content.strip():
        print(f"  [{lang} run {run_idx}] EMPTY CONTENT (likely token limit or reasoning)")

    parsed, _ = safe_parse_json(content)
    valid_json = parsed is not None
    schema_ok = check_schema_ok(parsed)
    reply = str(parsed.get("reply", "")).strip() if isinstance(parsed, dict) else ""
    language_ok = check_language_ok(lang, reply)
    style_ok = check_style_ok(reply)
    kb_ok = check_kb_ok(lang, reply)
    kb_str = str(kb_ok) if kb_ok is not None else "n/a"
    preview = reply.replace("\n", " ")[:120]

    line = (
        f"[{lang} run {run_idx}] latency={latency:.2f}s valid_json={valid_json} "
        f"schema_ok={schema_ok} language_ok={language_ok} style_ok={style_ok} "
        f"kb_ok={kb_str} reply={preview!r}"
    )
    print(line)

    return {
        "lang": lang,
        "latency": latency,
        "valid_json": valid_json,
        "schema_ok": schema_ok,
        "language_ok": language_ok,
        "style_ok": style_ok,
        "kb_ok": kb_ok,
        "reply": reply,
        "error": None,
        "line": line,
    }


# Aggregate run metrics, compute PASS/FAIL verdict, and write the markdown report.
def build_and_save_report(model: str, args: argparse.Namespace, results: list[dict]) -> None:
    latencies = [r["latency"] for r in results if r["latency"] is not None]
    p50 = statistics.median(latencies) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0

    total = len(results)
    pct_valid = (sum(1 for r in results if r["valid_json"]) / total * 100) if total else 0.0
    pct_schema = (sum(1 for r in results if r["schema_ok"]) / total * 100) if total else 0.0

    lang_pcts: dict[str, float] = {}
    for lang, _ in TEST_CASES:
        lang_rows = [r for r in results if r["lang"] == lang]
        lang_pcts[lang] = (sum(1 for r in lang_rows if r["language_ok"]) / len(lang_rows) * 100) if lang_rows else 0.0

    errors = [r["error"] for r in results if r["error"]]

    passed = (
        bool(latencies)
        and p50 <= 3.0
        and max_lat <= 6.0
        and pct_valid == 100.0
        and pct_schema == 100.0
        and lang_pcts.get("en", 0.0) == 100.0
        and lang_pcts.get("hi", 0.0) == 100.0
        and lang_pcts.get("hinglish", 0.0) == 100.0
        and len(errors) == 0
    )
    verdict = "PASS" if passed else "FAIL"

    first_replies: dict[str, str] = {}
    for r in results:
        if r["lang"] not in first_replies and r["reply"]:
            first_replies[r["lang"]] = r["reply"]

    summary_lines = [
        "",
        "=== SUMMARY ===",
        f"Model: {model} | Runs per case: {args.runs} | Effort: {args.effort} | JSON mode: {not args.no_json_mode}",
        f"p50 latency: {p50:.2f}s | max latency: {max_lat:.2f}s",
        f"valid_json: {pct_valid:.1f}% | schema_ok: {pct_schema:.1f}%",
        "language_ok: " + ", ".join(f"{lang}={lang_pcts[lang]:.1f}%" for lang, _ in TEST_CASES),
        f"Errors: {', '.join(errors) if errors else 'None'}",
        f"Verdict: {verdict}",
    ]
    for sline in summary_lines:
        print(sline)

    md_lines = [
        f"# Model Evaluation Results: `{model}`",
        "",
        f"- **Runs per case**: {args.runs}",
        f"- **Reasoning effort**: {args.effort}",
        f"- **JSON mode**: {not args.no_json_mode}",
        f"- **p50 latency**: {p50:.2f}s",
        f"- **max latency**: {max_lat:.2f}s",
        f"- **valid_json**: {pct_valid:.1f}%",
        f"- **schema_ok**: {pct_schema:.1f}%",
        "- **language_ok**: " + ", ".join(f"`{lang}` {lang_pcts[lang]:.1f}%" for lang, _ in TEST_CASES),
        f"- **Errors**: {', '.join(errors) if errors else 'None'}",
        f"- **Verdict**: **{verdict}**",
        "",
        "## Per-Call Output",
        "```text",
        *[r["line"] for r in results],
        "```",
        "",
        "## Full First Reply per Language",
    ]
    for lang, prompt_text in TEST_CASES:
        md_lines.append(f"### `{lang}`")
        md_lines.append(f"- **Prompt**: {prompt_text}")
        md_lines.append(f"- **Reply**: {first_replies.get(lang, '(no reply)')}")
        md_lines.append("")

    RESULTS_PATH.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\nSaved full results to {RESULTS_PATH}")


# Run one diagnostic request with explicit flags and return (ok, latency, error_label).
def run_diag_call(
    client: OpenAI,
    model: str,
    messages: list[dict],
    effort: str,
    send_effort: bool,
    send_json_mode: bool,
    variant_label: str,
    run_idx: int,
) -> tuple[bool, float | None, str | None]:
    is_gemini = "gemini" in model.lower()
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }
    if is_gemini:
        kwargs["max_tokens"] = 1200
    else:
        kwargs["max_completion_tokens"] = 1200
    if send_effort:
        kwargs["extra_body"] = {"reasoning_effort": effort}
    if send_json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    start = time.perf_counter()
    try:
        response = client.chat.completions.create(**kwargs)
        latency = time.perf_counter() - start
        content = response.choices[0].message.content or ""
        parsed, err_type = safe_parse_json(content)
        if parsed is None:
            print(f"  [{variant_label} #{run_idx}] FAIL status=200 type={err_type} msg='Invalid JSON in content'")
            return False, latency, str(err_type)
        print(f"  [{variant_label} #{run_idx}] OK latency={latency:.2f}s")
        return True, latency, None
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", "n/a")
        err_type = type(exc).__name__
        err_msg = str(exc).replace("\n", " ")[:150]
        print(f"  [{variant_label} #{run_idx}] FAIL status={status_code} type={err_type} msg={err_msg!r}")
        return False, None, f"{status_code} {err_type}"


# Run A/B/C/D diagnostics (5 calls each, 2s gap) to isolate provider failure causes.
def run_diagnostics(client: OpenAI, model: str, effort: str) -> None:
    full_prompt = build_system_prompt(lead={}, available_slots=SAMPLE_SLOTS, language="en")
    short_prompt = full_prompt.split("KNOWLEDGE BASE:")[0] + "KNOWLEDGE BASE:\nBasic Rs 999, Pro Rs 2499."
    user_text = TEST_CASES[0][1]

    variants = [
        ("A) as-is", full_prompt, True, True),
        ("B) no reasoning_effort", full_prompt, False, True),
        ("C) no json_mode", full_prompt, True, False),
        ("D) no KB text", short_prompt, True, True),
    ]

    summary_rows: list[dict] = []
    total_calls = len(variants) * 5
    call_num = 0

    for label, sys_prompt, use_effort, use_json in variants:
        print(f"\n--- Variant {label} ---")
        messages = build_chat_messages(sys_prompt, recent_messages=[], user_text=user_text)
        successes = 0
        latencies: list[float] = []
        errors: list[str] = []

        for run_idx in range(1, 6):
            call_num += 1
            ok, lat, err_label = run_diag_call(
                client, model, messages, effort, use_effort, use_json, label, run_idx
            )
            if ok:
                successes += 1
            if lat is not None:
                latencies.append(lat)
            if err_label:
                errors.append(err_label)
            if call_num < total_calls:
                time.sleep(2.0)

        p50_str = f"{statistics.median(latencies):.2f}s" if latencies else "n/a"
        common_err = Counter(errors).most_common(1)[0][0] if errors else "None"
        summary_rows.append(
            {
                "variant": label,
                "score": f"{successes}/5",
                "p50": p50_str,
                "error": common_err,
            }
        )

    print("\n=== DIAGNOSTIC SUMMARY ===")
    print(f"{'variant':<24} | {'successes/5':<11} | {'p50 latency':<11} | most common error")
    print("-" * 78)
    for row in summary_rows:
        print(
            f"{row['variant']:<24} | {row['score']:<11} | "
            f"{row['p50']:<11} | {row['error']}"
        )


# Run all multilingual test cases --runs times with a configurable pause between calls and print the report.
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    client, model, base_url = create_client(args)
    host = urlparse(base_url).netloc or base_url
    print(f"Provider: host={host} | model={model} | effort={args.effort}")
    if args.diag:
        run_diagnostics(client, model, args.effort)
        return
    use_json_mode = not args.no_json_mode

    results: list[dict] = []
    total_calls = len(TEST_CASES) * args.runs
    call_num = 0

    for lang, prompt_text in TEST_CASES:
        for run_idx in range(1, args.runs + 1):
            call_num += 1
            res = run_single_case(client, model, lang, prompt_text, run_idx, args.effort, use_json_mode)
            results.append(res)
            if call_num < total_calls:
                wait_seconds = max(args.delay, 10.0) if res["error"] == "429" else args.delay
                time.sleep(wait_seconds)

    build_and_save_report(model, args, results)


if __name__ == "__main__":
    main()
