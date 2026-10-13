@echo off
REM Desktop entry point: same flow as Auto-Job (run.py -> setup_and_verify_gemini).
REM 1) visible console to configure profile/model/thinking  2) proxy starts hidden  3) console closes.
chcp 65001 > nul
setlocal
cd /d "%~dp0"
title Gemini API Wrapper - Configuracao

set "PY=%~dp0venv\Scripts\python.exe"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

if not exist "%PY%" (
    echo [+] Criando ambiente virtual...
    python -m venv venv || goto FAIL
    "%PY%" -m pip install -e . || goto FAIL
    "%PY%" -m pip install fastapi uvicorn playwright pillow httpx || goto FAIL
    "%PY%" -m playwright install chromium || goto FAIL
)

"%PY%" -c "import curl_cffi, loguru, orjson, pydantic, uvicorn, fastapi, playwright, httpx, PIL" > nul 2>&1
if errorlevel 1 (
    echo [+] Garantindo pacotes do ambiente Gemini API...
    "%PY%" -m pip install -e . || goto FAIL
    "%PY%" -m pip install fastapi uvicorn playwright pillow httpx || goto FAIL
    "%PY%" -m playwright install chromium || goto FAIL
)

echo.
echo =======================================================================
echo      GEMINI API WRAPPER - CONFIGURACAO INICIAL DE PERFIL E MODELO
echo =======================================================================
"%PY%" select_model.py
if errorlevel 1 (
    echo.
    echo [ERRO] A configuracao do Gemini API Wrapper nao foi concluida com sucesso.
    goto FAIL
)

REM Start the proxy hidden unless one is already running or booting (e.g. started by Auto-Job)
powershell -NoProfile -Command "$m=$null; try{$m=[Threading.Mutex]::OpenExisting('Local\GeminiAPIWrapper_ProxyServer_Mutex')}catch{}; if($m){$m.Dispose()}; $l=[bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue); if($m -or $l){exit 0}else{exit 1}"
if errorlevel 1 (
    echo.
    echo [+] Iniciando o servidor Gemini API Proxy Server na porta 8000 ^(em segundo plano^)...
    wscript.exe "%~dp0Gemini Wrapper (hidden).vbs"
) else (
    echo.
    echo [+] Gemini API Proxy Server ja esta ativo ou em inicializacao na porta 8000.
)

<nul set /p "=[+] Aguardando o servidor responder na porta 8000 (pre-warming Playwright)..."
powershell -NoProfile -Command "$t=Get-Date; while(((Get-Date)-$t).TotalSeconds -lt 45){ try{ if((Invoke-WebRequest http://127.0.0.1:8000/docs -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200){exit 0} }catch{}; Write-Host -NoNewline '.'; Start-Sleep 1 }; exit 1"
if errorlevel 1 (
    echo.
    echo [AVISO] Servidor ainda inicializando; seguira em segundo plano.
) else (
    echo.
    echo [OK] Servidor Gemini API ativo, pronto e operacional na porta 8000!
)
echo Este terminal sera fechado; o servidor continua rodando oculto. Para parar: "Stop Gemini Wrapper.bat".
timeout /t 4 > nul
exit /b 0

:FAIL
echo.
pause
exit /b 1
