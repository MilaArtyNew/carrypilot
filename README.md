# Carrypilot

CarryPilot funding and carry trading operator bot for monitoring opportunities and managing paper/live carry positions. It tracks balances, opportunities, positions, executor actions, pause/resume state, and runtime statistics.

## Features

- Scans funding/carry opportunities and reports operator-ready candidates.
- Provides Telegram controls for balances, opportunities, positions, settings, pause/resume, stats, and close actions.
- Separates paper/live ledgers and exchange/private-key configuration from Git.

## Architecture

- **Repository:** `MilaArtyNew/carrypilot`
- **Primary stack:** Python, Docker, systemd
- **Entrypoints and scripts:**
  - `main.py`
- **Notable dependencies:** `aiohttp`, `base58`, `cryptography`, `fast-stark-crypto`, `nado-protocol`, `protobuf`, `python-dotenv`, `python-telegram-bot`

## Configuration

Configure the service with environment variables. Do not commit real secrets to the repository.

- `DATA_DIR` — required or optional runtime configuration. See deployment environment for the actual value.
- `EXTENDED_API_KEY` — required or optional runtime configuration. See deployment environment for the actual value.
- `EXTENDED_PRIVATE_KEY` — required or optional runtime configuration. See deployment environment for the actual value.
- `EXTENDED_PUBLIC_KEY` — required or optional runtime configuration. See deployment environment for the actual value.
- `LEVERAGE` — required or optional runtime configuration. See deployment environment for the actual value.
- `MAX_BID_ASK_SPREAD` — required or optional runtime configuration. See deployment environment for the actual value.
- `MIN_FUNDING_SPREAD` — required or optional runtime configuration. See deployment environment for the actual value.
- `MIN_MINUTES_TO_FUNDING` — required or optional runtime configuration. See deployment environment for the actual value.
- `MIN_NET_PROFIT` — required or optional runtime configuration. See deployment environment for the actual value.
- `NADO_PRIVATE_KEY` — required or optional runtime configuration. See deployment environment for the actual value.
- `NADO_WALLET_ADDRESS` — required or optional runtime configuration. See deployment environment for the actual value.
- `PAPER_TRADING` — required or optional runtime configuration. See deployment environment for the actual value.
- `POSITION_MARGIN_USD` — required or optional runtime configuration. See deployment environment for the actual value.
- `SCAN_INTERVAL` — required or optional runtime configuration. See deployment environment for the actual value.
- `TELEGRAM_BOT_TOKEN` — required or optional runtime configuration. See deployment environment for the actual value.
- `TELEGRAM_CHAT_ID` — required or optional runtime configuration. See deployment environment for the actual value.
- `ZERO_ONE_PRIVATE_KEY` — required or optional runtime configuration. See deployment environment for the actual value.

## Setup

```bash
git clone https://github.com/MilaArtyNew/carrypilot
cd carrypilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running Locally

```bash
python main.py
```

## Bot Commands

- `/balances` — Show balances.
- `/close` — Run the close workflow for this project.
- `/closeall` — Close all managed positions.
- `/exchanges` — List configured exchanges.
- `/log` — Show recent logs.
- `/opportunities` — Show detected opportunities.
- `/pause` — Pause automation.
- `/resume` — Resume automation.
- `/settings` — Show or change settings.
- `/skipall` — Skip all pending opportunities/actions.
- `/stats` — Show runtime or trading statistics.
- `/status` — Show current service or strategy status.

If a command requires extra input and the argument is missing, the bot should ask a follow-up question instead of failing silently.

## Deployment Notes

- Keep secrets in the deployment platform environment variables, not in Git.
- Use the default branch as the source of truth for deployments.
- Check logs after every deployment and verify the `/status` or health endpoint when available.
- If the project uses a scheduler, verify timezone assumptions and idempotency before enabling it in production.

## Operational Notes

- Review logs after startup for missing environment variables or API authentication errors.
- Keep command names in English and document every user-facing command in this README.
- For Telegram bots, `/help` should list the same commands documented here.
- Inline buttons should edit the original message with the final status rather than sending duplicate messages.

## Troubleshooting

- **Bot does not respond:** verify the bot token, webhook/polling mode, and chat permissions.
- **Missing data:** check API keys, rate limits, and upstream service status.
- **Deployment starts but exits:** inspect platform logs for missing environment variables or import errors.
- **Commands differ from README:** update the command list here and in the bot command menu at the same time.

## Security

- Never commit `.env` files, API keys, private keys, Telegram tokens, or session strings.
- Use `.env.example` for placeholders only.
- Rotate any credential that was accidentally committed.
