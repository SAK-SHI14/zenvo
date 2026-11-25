import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Explicit allowlist of source, template, static, KB, test, tool, and config files.
ALLOWLIST_FILES = (
    "README.md",
    "REPORT.md",
    "architecture.md",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    "run.bat",
    "app/__init__.py",
    "app/brain.py",
    "app/dashboard.py",
    "app/db.py",
    "app/handlers.py",
    "app/main.py",
    "app/router.py",
    "app/seed.py",
    "app/summary.py",
    "templates/call.html",
    "templates/call_detail.html",
    "templates/dashboard.html",
    "static/call.css",
    "static/call.js",
    "static/dashboard.css",
    "kb/faqs.md",
    "kb/features.md",
    "kb/onboarding.md",
    "kb/pricing.md",
    "kb/refund_policy.md",
    "kb/security.md",
    "tests/test_smoke.py",
    "tests/test_call_ui.js",
    "tools/package_project.py",
    "tools/real_run_check.py",
    "tools/reset_demo_db.py",
    "tools/test_failover.py",
    "tools/test_one_model.py",
)

FORBIDDEN_PATTERNS = (
    ".env",
    ".db",
    ".bak",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".git/",
    ".zip",
    ".log",
    "test_one_model_results.md",
)


# Check that every allowlisted file exists before writing the submission archive.
def verify_allowlist() -> list[Path]:
    missing: list[str] = []
    paths: list[Path] = []
    for rel in ALLOWLIST_FILES:
        full_path = PROJECT_ROOT / rel
        if not full_path.is_file():
            missing.append(rel)
        else:
            paths.append(full_path)
    if missing:
        print(f"Error: missing allowlisted files: {', '.join(missing)}")
        sys.exit(1)
    return paths


# Inspect the created archive entries to guarantee no excluded files or secrets slipped in.
def verify_archive(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

    for name in names:
        normalized = name.replace("\\", "/")
        for forbidden in FORBIDDEN_PATTERNS:
            if forbidden == ".env":
                basename = normalized.rsplit("/", 1)[-1]
                if basename == ".env" or (basename.startswith(".env.") and basename != ".env.example"):
                    raise RuntimeError(f"Forbidden secret file found in archive: {name}")
            elif forbidden in normalized:
                raise RuntimeError(f"Forbidden artifact found in archive: {name}")
    return names


# Package the explicit allowlist into a clean ZIP placed outside the repository folder.
def build_submission_zip() -> Path:
    files = verify_allowlist()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"{PROJECT_ROOT.name}-submission-{timestamp}.zip"
    output_zip = PROJECT_ROOT.parent / archive_name
    top_folder = PROJECT_ROOT.name

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            rel_path = file_path.relative_to(PROJECT_ROOT).as_posix()
            arcname = f"{top_folder}/{rel_path}"
            zf.write(file_path, arcname=arcname)

    entries = verify_archive(output_zip)
    size_kb = output_zip.stat().st_size / 1024

    print(f"Created clean submission archive: {output_zip}")
    print(f"Archive size: {size_kb:.1f} KB | Total files: {len(entries)}")
    print("Included files:")
    for entry in entries:
        print(f"  - {entry}")
    return output_zip


if __name__ == "__main__":
    build_submission_zip()
