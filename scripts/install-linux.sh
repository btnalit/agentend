#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${1:-/opt/agentend}"
REPO_URL="${AGENTEND_REPO_URL:-https://github.com/btnalit/agentend.git}"
INSTALL_SPEC="${AGENTEND_INSTALL_SPEC:-.}"
SETUP_MODE="${AGENTEND_SETUP:-auto}"
START_SERVICES="${AGENTEND_START_SERVICES:-1}"
AGENTEND_BIN="$APP_HOME/bin/agentend"
AGENTEND_PYTHON="python3"

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

ensure_env_file() {
  if [ ! -f "$APP_HOME/.env" ] && [ -f "$APP_HOME/.env.example" ]; then
    cp "$APP_HOME/.env.example" "$APP_HOME/.env"
  fi
}

should_run_setup() {
  if [ "$SETUP_MODE" = "0" ] || [ "$SETUP_MODE" = "false" ] || [ "$SETUP_MODE" = "skip" ]; then
    return 1
  fi
  if [ "$SETUP_MODE" = "1" ] || [ "$SETUP_MODE" = "true" ]; then
    if [ ! -t 0 ]; then
      echo "Interactive setup requires a TTY. Skipping prompts."
      return 1
    fi
    return 0
  fi
  [ -t 0 ]
}

run_interactive_setup() {
  echo
  echo "AgentEnd basic setup"
  echo "Press Enter to accept defaults. Optional integrations can be skipped."
  setup_llm
  setup_telegram
  setup_search
  echo
  echo "Configuration summary:"
  "$AGENTEND_BIN" llm current --home "$APP_HOME" || true
  "$AGENTEND_BIN" secrets list --home "$APP_HOME" || true
  echo
  echo "Running basic checks:"
  "$AGENTEND_BIN" llm test --home "$APP_HOME" || echo "LLM test failed. Check provider settings or API key."
  "$AGENTEND_BIN" doctor --home "$APP_HOME" || true
}

setup_llm() {
  echo
  echo "LLM provider:"
  echo "  1) fake - offline smoke test only (default)"
  echo "  2) deepseek - OpenAI-compatible DeepSeek API"
  echo "  3) openai - OpenAI API"
  echo "  4) custom - any OpenAI-compatible endpoint"
  read -r -p "Choose provider [fake]: " choice
  choice="${choice:-fake}"
  case "$choice" in
    1|fake|skip)
      "$AGENTEND_BIN" llm set --home "$APP_HOME" --provider fake --model fake-llm
      ;;
    2|deepseek)
      prompt_default model "Model" "deepseek-v4-flash"
      prompt_default base_url "Base URL" "https://api.deepseek.com"
      prompt_default api_key_env "API key env name" "DEEPSEEK_API_KEY"
      prompt_secret secret "Paste $api_key_env, or press Enter to skip"
      set_env_value "$api_key_env" "$secret"
      "$AGENTEND_BIN" llm set --home "$APP_HOME" --provider deepseek --model "$model" --base-url "$base_url" --api-key-env "$api_key_env"
      ;;
    3|openai)
      prompt_default model "Model" "gpt-4.1"
      prompt_default base_url "Base URL" "https://api.openai.com/v1"
      prompt_default api_key_env "API key env name" "OPENAI_API_KEY"
      prompt_secret secret "Paste $api_key_env, or press Enter to skip"
      set_env_value "$api_key_env" "$secret"
      "$AGENTEND_BIN" llm set --home "$APP_HOME" --provider openai --model "$model" --base-url "$base_url" --api-key-env "$api_key_env"
      ;;
    4|custom)
      prompt_required provider "Provider name"
      prompt_required model "Model"
      prompt_required base_url "OpenAI-compatible base URL"
      default_env="$(printf '%s_API_KEY' "$provider" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9_' '_')"
      prompt_default api_key_env "API key env name" "$default_env"
      prompt_secret secret "Paste $api_key_env, or press Enter to skip"
      set_env_value "$api_key_env" "$secret"
      "$AGENTEND_BIN" llm set --home "$APP_HOME" --provider "$provider" --model "$model" --base-url "$base_url" --api-key-env "$api_key_env"
      ;;
    *)
      echo "Unknown choice. Keeping fake provider."
      "$AGENTEND_BIN" llm set --home "$APP_HOME" --provider fake --model fake-llm
      ;;
  esac
}

setup_telegram() {
  echo
  prompt_secret token "Paste TELEGRAM_BOT_TOKEN, or press Enter to skip Telegram"
  set_env_value "TELEGRAM_BOT_TOKEN" "$token"
}

setup_search() {
  echo
  read -r -p "Enable Brave Search? [y/N]: " enable_brave
  case "$enable_brave" in
    y|Y|yes|YES)
      prompt_secret brave_key "Paste BRAVE_SEARCH_API_KEY, or press Enter to skip"
      set_env_value "BRAVE_SEARCH_API_KEY" "$brave_key"
      ;;
    *)
      echo "Brave Search skipped."
      ;;
  esac
}

prompt_default() {
  var_name="$1"
  label="$2"
  default_value="$3"
  read -r -p "$label [$default_value]: " value
  printf -v "$var_name" "%s" "${value:-$default_value}"
}

prompt_required() {
  var_name="$1"
  label="$2"
  value=""
  while [ -z "$value" ]; do
    read -r -p "$label: " value
  done
  printf -v "$var_name" "%s" "$value"
}

prompt_secret() {
  var_name="$1"
  label="$2"
  if [ -t 0 ]; then
    printf "%s: " "$label"
    stty -echo
    IFS= read -r value
    stty echo
    printf "\n"
  else
    value=""
  fi
  printf -v "$var_name" "%s" "$value"
}

set_env_value() {
  key="$1"
  value="$2"
  [ -n "$key" ] || return 0
  [ -n "$value" ] || return 0
  "$AGENTEND_PYTHON" - "$APP_HOME/.env" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
next_lines = []
replaced = False
for line in lines:
    if line.startswith(f"{key}="):
        next_lines.append(f"{key}={value}")
        replaced = True
    else:
        next_lines.append(line)
if not replaced:
    next_lines.append(f"{key}={value}")
path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
PY
}

env_value_present() {
  key="$1"
  [ -f "$APP_HOME/.env" ] || return 1
  grep -Eq "^[[:space:]]*$key=[^[:space:]#]+" "$APP_HOME/.env"
}

telegram_configured() {
  env_value_present "TELEGRAM_BOT_TOKEN"
}

should_start_services() {
  [ "$START_SERVICES" != "0" ] && [ "$START_SERVICES" != "false" ] && [ "$START_SERVICES" != "skip" ]
}

systemd_available() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

openwrt_available() {
  [ -d /etc/init.d ] && {
    command -v procd >/dev/null 2>&1 || [ -x /sbin/procd ] || grep -qi openwrt /etc/os-release 2>/dev/null
  }
}

write_root_file() {
  target="$1"
  mode="${2:-0644}"
  tmp="$(mktemp)"
  cat > "$tmp"
  as_root cp "$tmp" "$target"
  as_root chmod "$mode" "$target"
  rm -f "$tmp"
}

ensure_python_base_modules() {
  if python3 - <<'PY' >/dev/null 2>&1
import sqlite3
import ssl
PY
  then
    return
  fi
  cat >&2 <<'EOF'
Python is missing required standard-library modules.

On OpenWrt, install the split Python packages, then re-run:
  opkg update
  opkg install python3-pip python3-sqlite3 python3-ssl ca-bundle
EOF
  exit 1
}

install_python_runtime() {
  ensure_python_base_modules
  if python3 -m venv .venv; then
    install_venv_runtime
  else
    install_target_runtime
  fi
  create_agentend_wrapper
  install_shell_command
}

install_venv_runtime() {
  . .venv/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -e "$INSTALL_SPEC"
  AGENTEND_PYTHON="$APP_HOME/.venv/bin/python"
}

install_target_runtime() {
  echo "Python venv is not available; using local target runtime under $APP_HOME/.deps."
  if ! python3 -m pip --version >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Python pip is not available.

On OpenWrt, install pip, then re-run:
  opkg update
  opkg install python3-pip
EOF
    exit 1
  fi
  if [ "$INSTALL_SPEC" != "." ]; then
    echo "AGENTEND_INSTALL_SPEC=$INSTALL_SPEC is ignored without venv; installing the runtime dependency set only."
  fi
  mkdir -p "$APP_HOME/.deps"
  python3 -m pip install --target "$APP_HOME/.deps" --upgrade \
    "httpx>=0.27" \
    "pydantic>=2" \
    "PyYAML>=6" \
    "SQLAlchemy>=2" \
    "typer>=0.12" \
    "python-telegram-bot>=21" \
    "mcp>=1.0" \
    "starlette>=0.40,<0.48"
  AGENTEND_PYTHON="python3"
}

create_agentend_wrapper() {
  mkdir -p "$APP_HOME/bin"
  cat > "$AGENTEND_BIN" <<EOF
#!/usr/bin/env sh
APP_HOME="$APP_HOME"
if [ -x "\$APP_HOME/.venv/bin/agentend" ]; then
  exec "\$APP_HOME/.venv/bin/agentend" "\$@"
fi
export PYTHONPATH="\$APP_HOME/src:\$APP_HOME/.deps\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -c 'from agentend.cli import main; main()' "\$@"
EOF
  chmod +x "$AGENTEND_BIN"
}

install_shell_command() {
  target_dir=""
  if [ -d /usr/local/bin ]; then
    target_dir="/usr/local/bin"
  elif [ -d /usr/bin ]; then
    target_dir="/usr/bin"
  fi
  if [ -z "$target_dir" ]; then
    echo "No system PATH directory found; use $AGENTEND_BIN directly."
    return
  fi
  if [ -w "$target_dir" ]; then
    ln -sf "$AGENTEND_BIN" "$target_dir/agentend"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo ln -sf "$AGENTEND_BIN" "$target_dir/agentend" || echo "Could not install agentend command; use $AGENTEND_BIN directly."
    return
  fi
  echo "Could not install agentend command in $target_dir; use $AGENTEND_BIN directly."
}

install_background_services() {
  if ! should_start_services; then
    echo "Background service startup skipped by AGENTEND_START_SERVICES=$START_SERVICES."
    return
  fi
  if systemd_available; then
    install_systemd_services
    return
  fi
  if openwrt_available; then
    install_openwrt_services
    return
  fi
  start_fallback_background_services
}

install_systemd_services() {
  echo "Installing and starting systemd services."
  write_root_file /etc/systemd/system/agentend-worker.service 0644 <<EOF
[Unit]
Description=AgentEnd Worker
After=network.target

[Service]
WorkingDirectory=$APP_HOME
EnvironmentFile=-$APP_HOME/.env
ExecStart=$AGENTEND_BIN serve --home $APP_HOME
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  write_root_file /etc/systemd/system/agentend-telegram.service 0644 <<EOF
[Unit]
Description=AgentEnd Telegram Bot
After=network.target

[Service]
WorkingDirectory=$APP_HOME
EnvironmentFile=-$APP_HOME/.env
ExecStart=$AGENTEND_BIN telegram serve --home $APP_HOME
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  as_root systemctl daemon-reload
  as_root systemctl enable --now agentend-worker.service
  if telegram_configured; then
    as_root systemctl enable --now agentend-telegram.service
  else
    echo "TELEGRAM_BOT_TOKEN is empty; agentend-telegram.service installed but not started."
  fi
  as_root systemctl --no-pager --full status agentend-worker.service || true
  if telegram_configured; then
    as_root systemctl --no-pager --full status agentend-telegram.service || true
  fi
}

install_openwrt_services() {
  echo "Installing and starting OpenWrt procd services."
  write_root_file /etc/init.d/agentend-worker 0755 <<EOF
#!/bin/sh /etc/rc.common

START=95
STOP=10
USE_PROCD=1

APP_HOME="$APP_HOME"

start_service() {
  procd_open_instance
  procd_set_param command "\$APP_HOME/bin/agentend" serve --home "\$APP_HOME"
  procd_set_param dir "\$APP_HOME"
  procd_set_param respawn 3600 5 5
  procd_close_instance
}
EOF

  write_root_file /etc/init.d/agentend-telegram 0755 <<EOF
#!/bin/sh /etc/rc.common

START=96
STOP=10
USE_PROCD=1

APP_HOME="$APP_HOME"

start_service() {
  procd_open_instance
  procd_set_param command "\$APP_HOME/bin/agentend" telegram serve --home "\$APP_HOME"
  procd_set_param dir "\$APP_HOME"
  procd_set_param respawn 3600 5 5
  procd_close_instance
}
EOF

  as_root /etc/init.d/agentend-worker enable
  as_root /etc/init.d/agentend-worker restart
  if telegram_configured; then
    as_root /etc/init.d/agentend-telegram enable
    as_root /etc/init.d/agentend-telegram restart
  else
    echo "TELEGRAM_BOT_TOKEN is empty; /etc/init.d/agentend-telegram installed but not started."
  fi
}

start_fallback_background_services() {
  echo "No systemd or OpenWrt procd detected; starting fallback background processes."
  mkdir -p "$APP_HOME/data/logs"
  start_fallback_service "agentend-worker" "$AGENTEND_BIN" serve --home "$APP_HOME"
  if telegram_configured; then
    start_fallback_service "agentend-telegram" "$AGENTEND_BIN" telegram serve --home "$APP_HOME"
  else
    echo "TELEGRAM_BOT_TOKEN is empty; Telegram background process not started."
  fi
}

start_fallback_service() {
  name="$1"
  shift
  pid_file="$APP_HOME/data/logs/$name.pid"
  log_file="$APP_HOME/data/logs/$name.log"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name already running pid=$(cat "$pid_file")"
    return
  fi
  nohup "$@" > "$log_file" 2>&1 &
  echo "$!" > "$pid_file"
  echo "$name started pid=$(cat "$pid_file") log=$log_file"
}

main() {
  if [ ! -d "$APP_HOME" ]; then
    as_root mkdir -p "$APP_HOME"
    as_root chown "${USER:-root}":"${USER:-root}" "$APP_HOME"
  fi

  if [ ! -d "$APP_HOME/.git" ]; then
    git clone "$REPO_URL" "$APP_HOME"
  fi

  cd "$APP_HOME"
  install_python_runtime
  "$AGENTEND_BIN" init --home "$APP_HOME"
  ensure_env_file

  if should_run_setup; then
    run_interactive_setup
  else
    echo "Interactive setup skipped. Re-run with AGENTEND_SETUP=1 bash scripts/install-linux.sh $APP_HOME to configure providers."
  fi
  install_background_services

  echo
  echo "AgentEnd initialized at $APP_HOME."
  echo "Run checks with: $AGENTEND_BIN doctor --home $APP_HOME"
  if command -v systemctl >/dev/null 2>&1; then
    echo "Systemd services: agentend-worker.service and agentend-telegram.service"
  else
    echo "OpenWrt services: /etc/init.d/agentend-worker and /etc/init.d/agentend-telegram when procd is available."
    echo "Fallback logs: $APP_HOME/data/logs/agentend-worker.log and agentend-telegram.log"
  fi
}

main "$@"
