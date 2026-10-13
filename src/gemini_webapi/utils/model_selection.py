import os
import sys
import asyncio
from typing import List, Tuple, Any, Optional
from .profile_utils import (
    get_all_profiles,
    get_active_profiles,
    save_active_profiles,
    resolve_paths,
    load_cookies_from_json,
    SELECTED_PROFILE_FILE,
    SELECTED_MODEL_FILE,
    SELECTED_THINKING_FILE,
)


def prompt_use_defaults() -> bool:
    """
    Asks the user if they want to use all default settings at terminal startup.
    Returns True if defaults should be used, False to run full interactive setup.
    """
    if "--default" in sys.argv or "-y" in sys.argv:
        print("\n[INFO] Flag detectada: utilizando todas as configurações padrão automaticamente.")
        return True

    print("\n======================================================================")
    print("                     INICIALIZAÇÃO DO SISTEMA                         ")
    print("======================================================================")
    print(" Deseja usar todas as configurações padrão?")
    print(" [Pressione Enter ou digite 's' para tudo padrão / 'n' para personalizar]")
    print("----------------------------------------------------------------------")
    try:
        choice = input(" Usar configurações padrão? (S/n) [Padrão: S]: ").strip().lower()
        if choice in ("n", "nao", "não", "no"):
            print("[INFO] Modo personalizado selecionado. Iniciando assistente passo a passo...\n")
            return False

        print("[INFO] Modo padrão selecionado. Aplicando opções recomendadas...\n")
        return True
    except (KeyboardInterrupt, EOFError):
        print("\n[!] Operação cancelada pelo usuário.")
        sys.exit(0)
    except Exception:
        return True


def select_profiles(profiles: List[str], current_active: List[str], use_defaults: bool) -> List[str]:
    """
    Resolves active profiles either automatically (default) or interactively.
    """
    default_profiles = [p for p in current_active if p in profiles] or [profiles[0]]

    if use_defaults:
        print(f"[INFO] Perfis ativos selecionados (padrão): {', '.join(default_profiles)}")
        return default_profiles

    print("\n======================================================================")
    print("             SELECIONAR PERFIL(IS) NAVEGADOR PARA O SERVIDOR          ")
    print("======================================================================")
    print(" Perfis disponiveis:")
    for idx, prof in enumerate(profiles, start=1):
        status = " [ATIVO]" if prof in current_active else ""
        print(f"  {idx}. {prof}{status}")
    print("----------------------------------------------------------------------")
    print(" Digite os numeros dos perfis desejados separados por virgula (ex: 1, 3),")
    print(" 'todos' ou '*' para ativar todos, ou Pressione Enter para manter os atuais.")
    print("----------------------------------------------------------------------")

    try:
        prof_choice = input("Escolha o(s) perfil(is) ativado(s) [Padrao: perfis atuais]: ").strip()
        if not prof_choice:
            return default_profiles
        if prof_choice.lower() in ("todos", "all", "*"):
            return list(profiles)

        selected = []
        parts = prof_choice.replace(" ", "").split(",")
        for part in parts:
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(profiles):
                    p_name = profiles[idx]
                    if p_name not in selected:
                        selected.append(p_name)
        if not selected:
            print("Opcao invalida. Mantendo perfis atuais.")
            return default_profiles
        return selected
    except Exception:
        print("Opcao invalida. Mantendo perfis atuais.")
        return default_profiles


async def verify_profiles_sessions(selected_profiles: List[str]) -> Tuple[List[str], Any]:
    """
    Verifies authentication for selected profiles and prompts for browser login if needed.
    """
    from gemini_webapi import GeminiClient
    from .cookie_sync import sync_account
    from gemini_webapi.constants import AccountStatus

    verified_profiles: List[str] = []
    primary_client = None

    for prof in selected_profiles:
        account_id, cookie_path, profile_path = resolve_paths(prof)
        psid, psidts = None, None
        if os.path.exists(cookie_path):
            psid, psidts = load_cookies_from_json(cookie_path)

        client = None
        is_authenticated = False

        if psid and psidts:
            print(f"Verificando sessao do perfil '{prof}'...")
            client = GeminiClient(psid, psidts)
            try:
                await client.init(timeout=15)
                if client.account_status != AccountStatus.UNAUTHENTICATED:
                    is_authenticated = True
                else:
                    print(f"A sessao do perfil '{prof}' esta expirada ou nao autenticada.")
            except Exception as e:
                print(f"Nao foi possivel conectar com os cookies do perfil '{prof}': {e}")

        if not is_authenticated:
            print(f"\n[AVISO] Perfil '{prof}' nao esta logado ou a sessao expirou.")
            try:
                login_choice = input(f"Deseja abrir o navegador para fazer login no perfil '{prof}' agora? (y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                login_choice = "n"

            if login_choice in ('y', 'yes'):
                print(f"\nAbrindo janela do navegador para o perfil '{prof}'...")
                print("Faca login na sua conta Google na janela aberta e depois feche o navegador.")
                success = await sync_account(prof, headless=False)
                if success and os.path.exists(cookie_path):
                    psid, psidts = load_cookies_from_json(cookie_path)
                    if psid and psidts:
                        client = GeminiClient(psid, psidts)
                        try:
                            await client.init(timeout=30)
                            if client.account_status != AccountStatus.UNAUTHENTICATED:
                                is_authenticated = True
                            else:
                                print(f"\n[ERRO] Login concluido mas a sessao do perfil '{prof}' continua invalida.")
                        except Exception as e:
                            print(f"\n[ERRO] Falha ao verificar sessao do perfil '{prof}': {e}")
                else:
                    print(f"\n[ERRO] Processo de login para '{prof}' falhou ou foi cancelado.")
            else:
                print(f"[INFO] Perfil '{prof}' desativado por falta de autenticacao.")

        if is_authenticated and client:
            verified_profiles.append(prof)
            if primary_client is None:
                primary_client = client
            print(f"[SUCESSO] Perfil '{prof}' verificado e ATIVADO!")

    if not verified_profiles:
        print("\n[ERRO CRITICO] Nenhum perfil selecionado foi autenticado com sucesso. Encerrando.")
        sys.exit(1)

    save_active_profiles(verified_profiles)
    print(f"\n[SUCESSO] Perfis ativos salvos no sistema: {', '.join(verified_profiles)}")
    print(f"Perfil primario ativo: '{verified_profiles[0]}'\n")
    return verified_profiles, primary_client


def select_model(client: Any, use_defaults: bool) -> Any:
    """
    Selects the active model either automatically (default) or interactively.
    """
    try:
        models = client.list_models()
    except Exception as e:
        print(f"\n[WARNING] Failed to list models from Gemini API: {e}")
        models = []

    if not models:
        print("\n[INFO] Using offline predefined model fallback registry.")
        class FallbackModel:
            def __init__(self, model_id, display_name, description):
                self.model_id = model_id
                self.display_name = display_name
                self.description = description

        models = [
            FallbackModel("gemini-3-flash", "Gemini 3.5 Flash", "All-around help / Fast responses"),
            FallbackModel("gemini-3-flash-thinking", "Gemini 3.5 Flash Thinking", "Solves complex problems / Deep reasoning"),
            FallbackModel("gemini-3-pro", "Gemini 3 Pro", "Standard pro tier model")
        ]

    models = sorted(models, key=lambda m: m.display_name)

    current_selection = ""
    if os.path.exists(SELECTED_MODEL_FILE):
        try:
            with open(SELECTED_MODEL_FILE, "r", encoding="utf-8") as f:
                current_selection = f.read().strip()
        except Exception:
            pass

    default_index = 1
    if current_selection:
        for idx, model in enumerate(models, start=1):
            if model.model_id == current_selection:
                default_index = idx
                break

    if use_defaults:
        selected_model = models[default_index - 1]
        print(f"[INFO] Modelo selecionado (padrão): {selected_model.display_name} ({selected_model.model_id})")
        return selected_model

    print("======================================================================")
    print("             AVAILABLE MODELS IN YOUR GEMINI ACCOUNT                  ")
    print("======================================================================")
    for idx, model in enumerate(models, start=1):
        print(f" {idx}. {model.display_name} ({model.model_id})")
        if model.description:
            print(f"    Description: {model.description}")
        print("----------------------------------------------------------------------")

    try:
        choice = input(f"Choose a model (1-{len(models)}) [Default: {default_index}]: ")
        if not choice.strip():
            selected_idx = default_index - 1
        else:
            selected_idx = int(choice) - 1
            if selected_idx < 0 or selected_idx >= len(models):
                raise ValueError()
    except (ValueError, IndexError):
        print("\nInvalid option. Using the default option.")
        selected_idx = default_index - 1

    selected_model = models[selected_idx]
    print(f"\n[SUCCESS] Selected model: {selected_model.display_name} ({selected_model.model_id})\n")
    return selected_model


def select_thinking_level(selected_model: Any, use_defaults: bool) -> str:
    """
    Selects thinking level (Standard / Extended) either automatically or interactively.
    """
    current_thinking = "Standard"
    if os.path.exists(SELECTED_THINKING_FILE):
        try:
            with open(SELECTED_THINKING_FILE, "r", encoding="utf-8") as f:
                current_thinking = f.read().strip()
        except Exception:
            pass

    default_thinking_idx = 1
    if current_thinking == "Extended":
        default_thinking_idx = 2

    # If the user selected a thinking model, default thinking level to Extended (2)
    if "thinking" in selected_model.model_id.lower() or "thinking" in selected_model.display_name.lower():
        default_thinking_idx = 2

    if use_defaults:
        selected_thinking = "Standard" if default_thinking_idx == 1 else "Extended"
        print(f"[INFO] Nível de Thinking (padrão): {selected_thinking}")
        return selected_thinking

    print("======================================================================")
    print("             CHOOSE THINKING LEVEL                                    ")
    print("======================================================================")
    print(" 1. Standard (Best for most questions / Fast responses / No CoT)")
    print(" 2. Extended (Complex problem solving / Deep thinking / With CoT)")
    print("----------------------------------------------------------------------")

    try:
        think_choice = input(f"Choose Thinking Level (1-2) [Default: {default_thinking_idx}]: ")
        if not think_choice.strip():
            selected_think_idx = default_thinking_idx - 1
        else:
            selected_think_idx = int(think_choice) - 1
            if selected_think_idx not in (0, 1):
                raise ValueError()
    except ValueError:
        print("\nInvalid option. Using the default option.")
        selected_think_idx = default_thinking_idx - 1

    selected_thinking = "Standard" if selected_think_idx == 0 else "Extended"
    print(f"\n[SUCCESS] Thinking Level configured to: {selected_thinking}\n")
    return selected_thinking


def select_headless_mode(use_defaults: bool) -> str:
    """
    Selects browser visibility (Headless / Headed) either automatically or interactively.
    """
    selected_headless_file = ".headless"
    current_headless = "True"
    if os.path.exists(selected_headless_file):
        try:
            with open(selected_headless_file, "r", encoding="utf-8") as f:
                current_headless = f.read().strip()
        except Exception:
            pass

    default_headless_choice = "n"
    if current_headless == "False":
        default_headless_choice = "y"

    if use_defaults:
        selected_headless = "False" if default_headless_choice == "y" else "True"
        vis_desc = "Visível (Headed)" if selected_headless == "False" else "Segundo plano (Headless)"
        print(f"[INFO] Visibilidade do navegador (padrão): {vis_desc}")
        return selected_headless

    print("======================================================================")
    print("             CHOOSE BROWSER VISIBILITY                                ")
    print("======================================================================")
    print(" Do you want the browser window to be visible during execution?")
    print(" [Standard: background mode (N) / Visible: headed mode (y)]")
    print("----------------------------------------------------------------------")

    prompt_suffix = " (Y/n)" if default_headless_choice == "y" else " (y/N)"
    try:
        headless_choice = input(f"Open browser window for visual monitoring?{prompt_suffix}: ").strip().lower()
        if not headless_choice:
            headless_choice = default_headless_choice

        if headless_choice in ('y', 'yes'):
            selected_headless = "False"
        else:
            selected_headless = "True"
    except Exception:
        selected_headless = "True"

    print(f"\n[SUCCESS] Browser visibility configured to: {'Visible (Headed)' if selected_headless == 'False' else 'Background (Headless)'}\n")
    return selected_headless


def save_selection_state(selected_model_id: str, selected_thinking: str, selected_headless: str) -> None:
    """
    Persists the chosen model, thinking level, and headless mode to disk.
    """
    try:
        with open(SELECTED_MODEL_FILE, "w", encoding="utf-8") as f:
            f.write(selected_model_id)
        with open(SELECTED_THINKING_FILE, "w", encoding="utf-8") as f:
            f.write(selected_thinking)
        with open(".headless", "w", encoding="utf-8") as f:
            f.write(selected_headless)
    except Exception as e:
        print(f"Error saving choices to disk: {e}")
        sys.exit(1)


async def run_model_selection() -> None:
    """
    Main entry point for Gemini model and profile selection.
    Prompts at startup if defaults should be used; if yes, selects all defaults directly.
    Otherwise, proceeds with the standard step-by-step interactive configuration.
    """
    use_defaults = prompt_use_defaults()

    profiles = get_all_profiles()
    current_active = get_active_profiles()

    selected_profiles = select_profiles(profiles, current_active, use_defaults=use_defaults)
    verified_profiles, primary_client = await verify_profiles_sessions(selected_profiles)

    selected_model = select_model(primary_client, use_defaults=use_defaults)
    selected_thinking = select_thinking_level(selected_model, use_defaults=use_defaults)
    selected_headless = select_headless_mode(use_defaults=use_defaults)

    save_selection_state(selected_model.model_id, selected_thinking, selected_headless)

    if use_defaults:
        print("======================================================================")
        print(" [SUCESSO] Inicialização concluída com configurações padrão!")
        print("======================================================================\n")
