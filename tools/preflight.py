"""Read-only pre-demo checker for Zenvo (Project-Sakshi-Placement).

Validates required files, environment configuration (without exposing API keys),
knowledge-base readability, Python dependencies, SQLite schema (if initialized),
port 8000 availability, Chrome location, and safe FastAPI app import.
"""

from __future__ import annotations

import importlib
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

REQUIRED_FILES = [
    "run.bat",
    "requirements.txt",
    ".env.example",
    "app/__init__.py",
    "app/main.py",
    "app/db.py",
    "app/brain.py",
    "app/router.py",
    "app/handlers.py",
    "app/summary.py",
    "app/dashboard.py",
    "app/seed.py",
    "templates/call.html",
    "templates/dashboard.html",
    "templates/call_detail.html",
    "static/call.js",
    "static/call.css",
    "static/dashboard.css",
]

KB_FILES = [
    "kb/pricing.md",
    "kb/features.md",
    "kb/faqs.md",
    "kb/onboarding.md",
    "kb/refund_policy.md",
    "kb/security.md",
]

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("openai", "openai"),
    ("python-dotenv", "dotenv"),
    ("jinja2", "jinja2"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
]

OPTIONAL_PACKAGES = [
    ("pytest", "pytest"),
]

EXPECTED_TABLES = {"leads", "sessions", "messages", "appointments", "calls"}

PLACEHOLDER_KEY_TOKENS = (
    "your-groq-api-key",
    "your-gemini-api-key",
    "your-api-key",
    "not-set",
    "changeme",
    "placeholder",
)


def is_placeholder_secret(value: str) -> bool:
    cleaned = value.strip().lower()
    if not cleaned:
        return True
    return any(token in cleaned for token in PLACEHOLDER_KEY_TOKENS)


def locate_chrome() -> str | None:
    for cmd in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(cmd)
        if found:
            return found

    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    for candidate in candidates:
        if str(candidate) and candidate.exists():
            return str(candidate)
    return None


def check_port_8000() -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        in_use = sock.connect_ex(("127.0.0.1", 8000)) == 0
    if in_use:
        return False, "Port 127.0.0.1:8000 is already accepting connections (server may already be running)."
    return True, "Port 127.0.0.1:8000 is free."


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    passes: list[str] = []

    def ok(msg: str) -> None:
        passes.append(msg)
        print(f"[OK]   {msg}")

    def warn(msg: str) -> None:
        warnings.append(msg)
        print(f"[WARN] {msg}")

    def fail(msg: str) -> None:
        errors.append(msg)
        print(f"[FAIL] {msg}")

    print("=== Zenvo Pre-Demo Preflight Check (Read-Only) ===")
    print(f"Project root: {ROOT_DIR}")
    print(f"Python      : {sys.version.split()[0]} ({sys.executable})\n")

    # 1. Required runtime packages
    missing_pkgs = []
    for pkg_name, import_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
        except Exception:
            missing_pkgs.append(pkg_name)
    if missing_pkgs:
        fail(f"Missing required Python packages: {', '.join(missing_pkgs)}")
    else:
        ok("All required Python runtime packages are importable.")

    for pkg_name, import_name in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(import_name)
            ok(f"Optional test package '{pkg_name}' is installed.")
        except Exception:
            warn(f"Optional test package '{pkg_name}' is not installed in this Python environment.")

    # 2. Required project files
    missing_files = [rel for rel in REQUIRED_FILES if not (ROOT_DIR / rel).is_file()]
    if missing_files:
        fail(f"Missing required project files: {', '.join(missing_files)}")
    else:
        ok(f"All {len(REQUIRED_FILES)} required application/UI files exist.")

    # 3. Knowledge base readability
    kb_issues = []
    for rel in KB_FILES:
        path = ROOT_DIR / rel
        if not path.is_file():
            kb_issues.append(f"{rel} (missing)")
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                kb_issues.append(f"{rel} (empty)")
        except Exception as exc:
            kb_issues.append(f"{rel} ({type(exc).__name__})")
    if kb_issues:
        fail(f"Knowledge-base issues: {', '.join(kb_issues)}")
    else:
        ok(f"All {len(KB_FILES)} knowledge-base Markdown files exist and are readable.")

    # 4. Environment configuration and API key presence (never print key values)
    env_path = ROOT_DIR / ".env"
    if env_path.is_file():
        ok(".env file is present.")
    else:
        warn(".env file is missing in project root (checking process environment variables only).")

    try:
        dotenv_mod = importlib.import_module("dotenv")
        if env_path.is_file():
            dotenv_mod.load_dotenv(dotenv_path=env_path, override=False)
    except Exception:
        pass

    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    db_path_raw = os.getenv("DB_PATH", "zenvo.db").strip() or "zenvo.db"

    if base_url:
        ok(f"LLM_BASE_URL is set ({base_url}).")
    else:
        fail("LLM_BASE_URL is missing or empty.")

    if model:
        ok(f"LLM_MODEL is set ({model}).")
    else:
        fail("LLM_MODEL is missing or empty.")

    if is_placeholder_secret(api_key):
        fail("LLM_API_KEY is missing or still set to a placeholder value (value hidden).")
    else:
        ok(f"LLM_API_KEY is configured (length={len(api_key)}, value hidden).")

    fb_url = os.getenv("LLM_FALLBACK_BASE_URL", "").strip()
    fb_model = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    fb_key = os.getenv("LLM_FALLBACK_API_KEY", "").strip()
    fb_any = any([fb_url, fb_model, fb_key])
    fb_all = bool(fb_url and fb_model and not is_placeholder_secret(fb_key))

    if fb_all:
        ok(f"Optional fallback provider configured ({fb_model}, fallback key length={len(fb_key)}, value hidden).")
    elif fb_any:
        warn("Partial fallback provider configuration detected (set all 3 LLM_FALLBACK_* vars or rely on scripted fallback).")
    else:
        warn("Optional fallback provider is not configured; primary failures will use scripted fallback.")

    # 5. Read-only SQLite database schema check
    db_file = Path(db_path_raw)
    if not db_file.is_absolute():
        db_file = (ROOT_DIR / db_file).resolve()

    if not db_file.exists():
        warn(f"SQLite DB '{db_file.name}' does not exist yet (FastAPI lifespan/init_db() will create it on first start).")
    else:
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                existing_tables = {row[0] for row in rows}
                missing_tables = sorted(EXPECTED_TABLES - existing_tables)
                if missing_tables:
                    fail(f"Database '{db_file.name}' is missing expected tables: {', '.join(missing_tables)}")
                else:
                    lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                    call_count = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                    appt_count = conn.execute(
                        "SELECT COUNT(*) FROM appointments WHERE status='confirmed'"
                    ).fetchone()[0]
                    ok(
                        f"Database '{db_file.name}' schema verified "
                        f"(leads={lead_count}, calls={call_count}, confirmed_appointments={appt_count})."
                    )
            finally:
                conn.close()
        except Exception as exc:
            fail(f"Could not inspect SQLite DB '{db_file.name}': {type(exc).__name__}: {exc}")

    # 6. Safe FastAPI import check (only if required packages didn't already fail)
    if not missing_pkgs:
        try:
            main_mod = importlib.import_module("app.main")
            fastapi_app = getattr(main_mod, "app", None)
            if fastapi_app is None:
                fail("app.main imported, but 'app' FastAPI object was not found.")
            else:
                ok(f"FastAPI application imported safely ({getattr(fastapi_app, 'title', 'Zenvo')}).")
        except Exception as exc:
            fail(f"Failed to import app.main safely: {type(exc).__name__}: {exc}")

    # 7. Port 8000 availability & browser lookup (non-fatal warnings)
    port_free, port_msg = check_port_8000()
    if port_free:
        ok(port_msg)
    else:
        warn(port_msg)

    chrome_path = locate_chrome()
    if chrome_path:
        ok(f"Browser found for Web Speech UI: {chrome_path}")
    else:
        warn("Chrome/Edge executable was not auto-detected (text fallback works in any browser).")

    print("\n=== Summary ===")
    print(f"Passed: {len(passes)} | Warnings: {len(warnings)} | Errors: {len(errors)}")
    if errors:
        print("Status: FAILED - resolve the [FAIL] item(s) above before starting the demo.")
        return 1
    if warnings:
        print("Status: READY WITH WARNINGS - review [WARN] item(s) above before presenting.")
    else:
        print("Status: READY - all preflight checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
