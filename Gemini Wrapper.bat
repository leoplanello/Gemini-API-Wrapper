@echo off
REM Standalone launcher: double-click to start the Gemini proxy (http://localhost:8000).
REM Creates the venv / installs dependencies on first run, then starts the server directly.
REM Use "Gemini Wrapper (hidden).vbs" to run it without a console window.
chcp 65001 > nul
setlocal
cd /d "%~dp0"
title Gemini API Wrapper (porta 8000)

set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [i] Criando ambiente virtual...
    python -m venv venv
    if errorlevel 1 goto FAIL
)

"%PY%" -c "import fastapi, uvicorn, playwright, curl_cffi" > nul 2>&1
if errorlevel 1 (
    echo [i] Instalando dependencias ^(primeira execucao^)...
    "%PY%" -m pip install -e . || goto FAIL
    "%PY%" -m pip install fastapi uvicorn playwright pillow || goto FAIL
    "%PY%" -m playwright install chromium || goto FAIL
)

if not exist .selected_profile (
    echo [i] Nenhum perfil selecionado. Abrindo assistente...
    "%PY%" select_model.py
)

echo [+] Gemini proxy em http://localhost:8000  ^(Ctrl+C para parar^)
"%PY%" proxy_server.py
exit /b %errorlevel%

:FAIL
echo.
echo [ERRO] Falha na preparacao do ambiente.
pause
exit /b 1
