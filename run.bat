@echo off
setlocal EnableExtensions

REM Always run relative to the repository folder (handles paths with spaces)
cd /d "%~dp0"

echo ==================================================
echo   Zenvo Autonomous AI Sales Voice Agent Launcher
echo ==================================================

REM Locate Python on PATH (or via the Windows py launcher)
set "SYS_PYTHON="
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    set "SYS_PYTHON=python"
) else (
    where py >nul 2>&1
    if %ERRORLEVEL%==0 (
        set "SYS_PYTHON=py -3"
    )
)

if "%SYS_PYTHON%"=="" (
    echo [ERROR] Python 3.11+ was not found on PATH.
    echo Please install Python from https://www.python.org/downloads/ and check "Add Python to PATH".
    goto :fail
)

REM Create local .venv and install dependencies on first run only
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment in .venv ...
    %SYS_PYTHON% -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv.
        goto :fail
    )

    echo [2/4] Installing dependencies from requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        goto :fail
    )
) else (
    echo [1/4] Using existing .venv environment.
)

REM Ensure .env exists; if missing, copy .env.example and ask user to add API keys first
if not exist ".env" (
    if not exist ".env.example" (
        echo [ERROR] Neither .env nor .env.example was found.
        goto :fail
    )
    copy ".env.example" ".env" >nul
    echo.
    echo [ACTION REQUIRED] Created .env from .env.example.
    echo Open .env, add your LLM_API_KEY ^(and optional LLM_FALLBACK_API_KEY^), save it, and run run.bat again.
    echo.
    pause
    exit /b 0
)

REM Initialize SQLite tables and indexes safely without resetting existing data
echo [3/4] Initializing SQLite database...
".venv\Scripts\python.exe" -c "from app.db import init_db; init_db()"
if errorlevel 1 (
    echo [ERROR] Database initialization failed.
    goto :fail
)

echo.
echo [4/4] Starting Zenvo FastAPI server...
echo   Call Page : http://127.0.0.1:8000/
echo   Dashboard : http://127.0.0.1:8000/dashboard
echo   API Docs  : http://127.0.0.1:8000/docs
echo   Press Ctrl+C to stop the server.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Server stopped.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
