#!/usr/bin/env bash
# Gemini Proxy Server - Setup and Control Center for Lubuntu/Linux

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

echo "======================================================================="
echo "             GEMINI API PROXY AND SPEECH TO TEXT SERVER"
echo "======================================================================="
echo ""

# -----------------------------------------------------------------------
# Step 1: Check Python Virtual Environment
# -----------------------------------------------------------------------
echo "[+] Verificando ambiente virtual Python..."
if [ ! -d "venv" ]; then
    echo "[i] Criando ambiente virtual em [venv]..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "[ERRO] Falha ao criar o ambiente virtual Python. Verifique se python3-venv esta instalado."
        exit 1
    fi
    FIRST_RUN=1
else
    echo "[i] Ambiente virtual encontrado."
fi

# -----------------------------------------------------------------------
# Step 2: Activate Virtual Environment
# -----------------------------------------------------------------------
source venv/bin/activate

# -----------------------------------------------------------------------
# Step 3: Install Dependencies on First Run
# -----------------------------------------------------------------------
update_deps() {
    echo "[+] Atualizando pacotes e dependencias..."
    pip install -e .
    if [ $? -ne 0 ]; then return 1; fi
    pip install fastapi uvicorn playwright pillow
    if [ $? -ne 0 ]; then return 1; fi
    python -m playwright install chromium
    if [ $? -ne 0 ]; then return 1; fi
    echo "[+] Dependencias instaladas com sucesso!"
    return 0
}

if [ -n "$FIRST_RUN" ]; then
    echo ""
    echo "[+] Primeira execucao detectada. Instalando dependencias..."
    update_deps
    if [ $? -ne 0 ]; then
        echo "[ERRO CRITICO] Ocorreu uma falha na instalacao das dependencias."
        exit 1
    fi
fi

# -----------------------------------------------------------------------
# Control Menu
# -----------------------------------------------------------------------
while true; do
    clear
    echo "======================================================================="
    echo "              GEMINI API PROXY - PAINEL DE CONTROLE (LUBUNTU/LINUX)"
    echo "======================================================================="
    echo ""
    echo "   1. Iniciar Servidor Proxy (FastAPI + Uvicorn)"
    echo "   2. Configurar Conta / Fazer Login no Gemini (sync_cookies)"
    echo "   3. Selecionar Modelo Ativo e Opcoes de Pensamento (select_model)"
    echo "   4. Gerenciar e Selecionar Perfis Ativos"
    echo "   5. Atualizar Dependencias e Playwright Chromium"
    echo "   6. Sair"
    echo ""
    echo "======================================================================="
    read -p "Escolha uma opcao (1-6) [Padrao: 1]: " choice
    choice=${choice:-1}

    case "$choice" in
        1)
            clear
            if [ ! -f ".selected_profile" ] && [ ! -f ".active_profiles" ]; then
                echo "[i] Nenhuma configuracao previa encontrada. Executando assistente de modelo..."
                python select_model.py
            fi
            echo "======================================================================="
            echo "[+] Iniciando o Servidor Proxy Local..."
            echo "O servidor ficara ouvindo em: http://localhost:8000"
            echo "Pressione Ctrl+C para interromper o servidor a qualquer momento."
            echo "======================================================================="
            echo ""
            uvicorn proxy_server:app --host 0.0.0.0 --port 8000
            echo ""
            read -p "Pressione Enter para voltar ao menu..."
            ;;
        2)
            clear
            echo "======================================================================="
            echo "[+] Configuracao e Verificacao de Contas Gemini"
            echo "======================================================================="
            echo ""
            python sync_cookies.py --verify-all
            echo ""
            read -p "Processo concluido. Pressione Enter para voltar ao menu..."
            ;;
        3|4)
            clear
            echo "======================================================================="
            echo "[+] Selecao de Modelo, Perfis e Modo de Operacao"
            echo "======================================================================="
            echo ""
            python select_model.py
            echo ""
            read -p "Pressione Enter para voltar ao menu..."
            ;;
        5)
            clear
            update_deps
            echo ""
            read -p "Pressione Enter para voltar ao menu..."
            ;;
        6)
            echo "Saindo..."
            exit 0
            ;;
        *)
            echo "Opcao invalida. Tente novamente."
            sleep 2
            ;;
    esac
done
