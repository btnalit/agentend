# AgentEnd Lite

Python single-agent workflow runtime with CLI, Telegram, SQLite, local workflows, and one-way MCP client integration.

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
agentend init
agentend llm set --provider openai --model gpt-4.1
agentend llm test
agentend chat
```

## Linux Deployment

Install into `/opt/agentend`:

```bash
bash scripts/install-linux.sh /opt/agentend
cp .env.example .env
```

Set `OPENAI_API_KEY` and `TELEGRAM_BOT_TOKEN` in `/opt/agentend/.env`.

Install the systemd service:

```bash
sudo cp deploy/agentend.service /etc/systemd/system/agentend.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentend
sudo journalctl -u agentend -f
```

Backup the local SQLite database:

```bash
agentend db backup --home /opt/agentend --output /opt/agentend/backups/agentend.sqlite
```
