@echo off
setlocal

cd /d "%~dp0"

echo =========================================
echo   Orb - Agentic
echo =========================================
echo.

if exist ".venv\Scripts\python.exe" goto validate_venv

python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if errorlevel 1 goto unsupported_python

echo Creating virtual environment...
python -m venv .venv
if errorlevel 1 exit /b 1
goto venv_ready

:validate_venv
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if errorlevel 1 goto stale_venv

:venv_ready

call .venv\Scripts\activate.bat
echo Installing dependencies...
pip install -q -r requirements.txt

if not exist "backend\data" mkdir backend\data

echo.
echo Starting server on http://localhost:8899
echo Press Ctrl+C to stop
echo.

REM Wait for the server to come up in the background, then open the browser once.
start "" /b cmd /c "for /l %%i in (1,1,60) do (curl -fsS -o nul http://localhost:8899 && (start "" http://localhost:8899 & exit) || timeout /t 1 /nobreak >nul)"

uvicorn backend.main:app --host 0.0.0.0 --port 8899 --reload
exit /b %errorlevel%

:unsupported_python
echo Error: Python 3.11 or newer is required.
exit /b 1

:stale_venv
echo Error: .venv uses Python older than 3.11 or is invalid.
echo Remove .venv and rerun this script with Python 3.11 or newer installed.
exit /b 1
