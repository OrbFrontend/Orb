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
if errorlevel 1 goto venv_creation_failed
goto venv_ready

:validate_venv
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if errorlevel 1 goto stale_venv

:venv_ready

call .venv\Scripts\activate.bat
echo Installing dependencies...
pip install -q -r requirements.txt
if errorlevel 1 goto pip_failed

if not exist "backend\data" mkdir backend\data

echo.
echo Starting server on http://localhost:8899
echo Press Ctrl+C to stop
echo.

REM Wait for the server to come up in the background, then open the browser once.
start "" /b cmd /c "for /l %%i in (1,1,60) do (curl -fsS -o nul http://localhost:8899 && (start "" http://localhost:8899 & exit) || timeout /t 1 /nobreak >nul)"

uvicorn backend.main:app --host 0.0.0.0 --port 8899 --reload
if errorlevel 1 goto uvicorn_failed
exit /b 0

:unsupported_python
echo.
echo [ERROR] Python 3.11 or newer is required.
goto end_error

:stale_venv
echo.
echo [ERROR] .venv uses Python older than 3.11 or is invalid.
echo Remove .venv and rerun this script with Python 3.11 or newer installed.
goto end_error

:venv_creation_failed
echo.
echo [ERROR] Failed to create virtual environment.
goto end_error

:pip_failed
echo.
echo [ERROR] Failed to install dependencies from requirements.txt.
goto end_error

:uvicorn_failed
echo.
echo [ERROR] Server terminated unexpectedly.
goto end_error

:end_error
echo.
echo Press any key to close this window...
pause >nul
exit /b 1