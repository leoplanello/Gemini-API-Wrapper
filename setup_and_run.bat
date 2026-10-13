@echo off
title Gemini Proxy Server - Setup and Control Center
chcp 65001 > nul
cls

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo =======================================================================
echo              GEMINI API PROXY AND SPEECH TO TEXT SERVER
echo =======================================================================
echo.

REM -----------------------------------------------------------------------
REM Step 1: Check Python Virtual Environment
REM -----------------------------------------------------------------------
echo [+] Verificando ambiente virtual Python...
if not exist venv (
    echo [i] Criando ambiente virtual em [venv]...
    python -m venv venv
    if errorlevel 1 (
        echo [ERRO] Falha ao criar o ambiente virtual Python. Verifique a instalacao do Python.
        goto ERROR_EXIT
    )
    set "FIRST_RUN=1"
) else (
    echo [i] Ambiente virtual encontrado.
)

REM -----------------------------------------------------------------------
REM Step 2: Activate Virtual Environment
REM -----------------------------------------------------------------------
set "VIRTUAL_ENV=%SCRIPT_DIR%venv"
set "PATH=%VIRTUAL_ENV%\Scripts;%PATH%"

REM -----------------------------------------------------------------------
REM Step 3: Install Dependencies on First Run
REM -----------------------------------------------------------------------
if defined FIRST_RUN (
    echo.
    echo [+] Primeira execucao detectada. Instalando dependencias...
    call :UPDATE_DEPS
    if errorlevel 1 goto ERROR_EXIT
)

:MENU
cls
echo =======================================================================
echo              GEMINI API PROXY - PAINEL DE CONTROLE
echo =======================================================================
echo.
echo   1. Iniciar Servidor Proxy (FastAPI + Uvicorn)
echo   2. Configurar Conta / Fazer Login no Gemini (sync_cookies)
echo   3. Selecionar Modelo Ativo e Opcoes de Pensamento (select_model)
echo   4. Gerenciar e Selecionar Perfis Ativos
echo   5. Atualizar Dependencias e Playwright Chromium
echo   6. Sair
echo.
echo =======================================================================
set "choice=1"
set /p choice="Escolha uma opcao (1-6) [Padrao: 1]: "

if "%choice%"=="1" goto START_SERVER
if "%choice%"=="2" goto SYNC_COOKIES
if "%choice%"=="3" goto SELECT_MODEL
if "%choice%"=="4" goto SELECT_MODEL
if "%choice%"=="5" goto UPDATE_DEPS_MENU
if "%choice%"=="6" goto NORMAL_EXIT

echo.
echo Opcao invalida. Tente novamente.
timeout /t 2 > nul
goto MENU


:SYNC_COOKIES
cls
echo =======================================================================
echo [+] Configuracao e Verificacao de Contas Gemini
echo =======================================================================
echo.
python sync_cookies.py --verify-all
echo.
echo Processo concluido.
pause
goto MENU

:SELECT_MODEL
cls
echo =======================================================================
echo [+] Selecao de Modelo e Modo de Operacao
echo =======================================================================
echo.
python select_model.py
if errorlevel 1 (
    echo.
    echo [AVISO] Nao foi possivel concluir a selecao do modelo.
)
echo.
pause
goto MENU

:UPDATE_DEPS_MENU
cls
call :UPDATE_DEPS
pause
goto MENU

:UPDATE_DEPS
echo [+] Atualizando pacotes e dependencias...
pip install -e .
if errorlevel 1 goto ERROR_DEPS
pip install fastapi uvicorn playwright pillow
if errorlevel 1 goto ERROR_DEPS
python -m playwright install chromium
if errorlevel 1 goto ERROR_DEPS
echo [+] Dependencias instaladas com sucesso!
exit /b 0

:ERROR_DEPS
echo [ERRO] Falha durante a instalacao das dependencias.
exit /b 1

:START_SERVER
cls
if not exist .selected_profile (
    echo [i] Nenhuma configuracao previa encontrada. Executando assistente de modelo...
    python select_model.py
)

echo =======================================================================
echo [+] Iniciando o Servidor Proxy Local...
echo O servidor ficara ouvindo em: http://localhost:8000
echo Pressione Ctrl+C para interromper o servidor a qualquer momento.
echo =======================================================================
echo.

uvicorn proxy_server:app --host 0.0.0.0 --port 8000
if errorlevel 1 (
    echo.
    echo [ERRO] O servidor encerrou com erro.
    echo Verifique se a porta 8000 ja esta em uso por outro processo.
) else (
    echo.
    echo [INFO] Servidor finalizado pelo usuario.
)

echo.
echo =======================================================================
echo O terminal permanecera aberto para leitura dos logs.
echo =======================================================================
pause
goto MENU

:ERROR_EXIT
echo.
echo [ERRO CRITICO] Ocorreu uma falha na execucao do script.
echo.
pause
exit /b 1

:NORMAL_EXIT
echo Saindo...
exit /b 0
