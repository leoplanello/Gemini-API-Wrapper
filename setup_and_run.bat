@echo off
title Gemini Proxy Server - Setup and Run
cls
echo =======================================================================
echo              GEMINI API PROXY AND SPEECH TO TEXT SERVER
echo =======================================================================
echo.

echo [+] Step 1: Verifying virtual environment...
if not exist venv (
    echo [i] Creating virtual environment [venv]...
    python -m venv venv
) else (
    echo [i] Virtual environment already exists.
)

echo.
echo [+] Step 2: Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo [+] Step 3: Updating dependencies...
echo [i] Installing gemini_webapi wrapper in development mode...
pip install -e .
echo.
echo [i] Installing proxy server dependencies (FastAPI, Uvicorn, etc.)...
pip install fastapi uvicorn playwright pillow
echo.
echo [i] Ensuring Playwright Chromium browser is installed...
python -m playwright install chromium

echo.
echo [+] Step 4: Starting interactive account configurator...
python sync_cookies.py --verify-all

echo.
echo [+] Step 5: Selecting active model...
python select_model.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Could not configure the Gemini model.
    echo The proxy server will not start.
    echo Verify that your cookies are valid or run: python sync_cookies.py cookies --login
    echo.
    pause
    exit /b %errorlevel%
)

echo.
echo =======================================================================
echo [+] Step 6: Starting the local proxy server...
echo The proxy will be listening at http://localhost:8000
echo =======================================================================
echo.
uvicorn proxy_server:app --host 0.0.0.0 --port 8000
