@echo off
setlocal

cd /d "%~dp0"

echo =========================================
echo   Orb - Agentic
echo =========================================
echo.

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
echo Installing dependencies...
pip install -q -r requirements.txt

if not exist "backend\data" mkdir backend\data

echo.
echo Starting server on http://localhost:8899
echo Press Ctrl+C to stop
echo.

uvicorn backend.main:app --host 0.0.0.0 --port 8899 --reload
