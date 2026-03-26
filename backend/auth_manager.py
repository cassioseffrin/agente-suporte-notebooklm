#!/usr/bin/env python3
"""
Gerenciador de autenticação NotebookLM para produção Debian 12.
Verifica validade da sessão e renova via transferência SCP do Mac.

Estrutura de sessão atual (notebooklm-py):
  Mac (origem): ~/.notebooklm/storage_state.json
  Servidor (destino): ~/.notebooklm/storage_state.json

Uso:
    source /opt/erp-agent/venv/bin/activate
    python auth_manager.py --mac-host 192.168.1.50 --mac-user seunome
"""

import subprocess
import os
import sys
from pathlib import Path
from datetime import datetime

# Caminho atualizado para a versão atual do notebooklm-py
SESSION_FILE    = Path.home() / ".notebooklm" / "storage_state.json"
LOG_FILE        = Path("/var/log/notebooklm_auth.log")
VENV_DIR        = Path("/opt/erp-agent/venv")
NOTEBOOKLM_BIN  = VENV_DIR / "bin" / "notebooklm"


def log(msg: str):
    timestamp = datetime.now().isoformat()
    line = f"[{timestamp}] {msg}"
    print(line)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except PermissionError:
        pass


def notebooklm_cmd() -> list[str]:
    if NOTEBOOKLM_BIN.exists():
        return [str(NOTEBOOKLM_BIN)]
    return ["notebooklm"]


def session_is_valid() -> bool:
    try:
        result = subprocess.run(
            notebooklm_cmd() + ["list"],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def renew_via_scp(mac_host: str, mac_user: str) -> bool:
    log(f"Transferindo sessão de {mac_user}@{mac_host}...")
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([
            "scp",
            f"{mac_user}@{mac_host}:~/.notebooklm/storage_state.json",
            str(SESSION_FILE)
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            log("Sessão transferida. Validando...")
            return session_is_valid()
        else:
            log(f"Falha no SCP: {result.stderr}")
            return False
    except Exception as e:
        log(f"Erro: {e}")
        return False


def check_and_renew(mac_host: str | None = None, mac_user: str | None = None):
    if session_is_valid():
        log("Sessão válida.")
        return True

    log("Sessão expirada. Iniciando renovação...")

    if mac_host and mac_user:
        success = renew_via_scp(mac_host, mac_user)
    else:
        log("⚠️  Sem mac_host/mac_user configurado.")
        log("    Renove manualmente copiando ~/.notebooklm/storage_state.json do Mac.")
        success = False

    if success:
        log("Renovação concluída.")
    else:
        log("Renovação falhou. Intervenção manual necessária.")
        sys.exit(1)

    return success


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NotebookLM Auth Manager")
    parser.add_argument("--mac-host", help="IP/hostname do Mac de dev")
    parser.add_argument("--mac-user", help="Usuário SSH no Mac")
    args = parser.parse_args()
    check_and_renew(mac_host=args.mac_host, mac_user=args.mac_user)