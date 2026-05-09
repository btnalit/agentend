#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${1:-/opt/agentend}"
REPO_URL="${AGENTEND_REPO_URL:-https://github.com/btnalit/agentend.git}"
INSTALL_SPEC="${AGENTEND_INSTALL_SPEC:-.}"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "This operation requires root privileges. Re-run as root or install sudo." >&2
    exit 1
  fi
}

if [ ! -d "$APP_HOME" ]; then
  as_root mkdir -p "$APP_HOME"
  as_root chown "${USER:-root}":"${USER:-root}" "$APP_HOME"
fi

if [ ! -d "$APP_HOME/.git" ]; then
  git clone "$REPO_URL" "$APP_HOME"
fi

cd "$APP_HOME"
if ! python3 -m venv .venv; then
  cat >&2 <<'EOF'
Python venv is not available in this interpreter.

Install the distro package that provides venv, then re-run this script.
Examples:
  Debian/Ubuntu: apt install python3-venv
  OpenWrt: opkg update && opkg list | grep -E '^python3.*venv'

AgentEnd's default Linux installer requires a venv so the system Python
environment is not modified.
EOF
  exit 1
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e "$INSTALL_SPEC"
agentend init --home "$APP_HOME"
if [ ! -f "$APP_HOME/.env" ] && [ -f "$APP_HOME/.env.example" ]; then
  cp "$APP_HOME/.env.example" "$APP_HOME/.env"
fi

echo "Edit $APP_HOME/.env, then install deploy/agentend.service with systemd."
