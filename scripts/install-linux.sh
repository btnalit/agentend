#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${1:-/opt/agentend}"
REPO_URL="${AGENTEND_REPO_URL:-https://github.com/btnalit/agentend.git}"

if [ ! -d "$APP_HOME" ]; then
  sudo mkdir -p "$APP_HOME"
  sudo chown "$USER":"$USER" "$APP_HOME"
fi

if [ ! -d "$APP_HOME/.git" ]; then
  git clone "$REPO_URL" "$APP_HOME"
fi

cd "$APP_HOME"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
agentend init --home "$APP_HOME"

echo "Edit $APP_HOME/.env, then install deploy/agentend.service with systemd."
