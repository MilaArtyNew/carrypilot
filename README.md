# CarryPilot

CarryPilot is a Telegram-based assistant for semi-automated funding-rate arbitrage between perpetual futures venues.

It monitors funding-rate differences across exchanges, filters opportunities by estimated net profit, liquidity, bid/ask spread, and time to funding, then sends actionable Telegram alerts. Trades are opened only after manual approval via Telegram.

> ⚠️ This is high-risk trading infrastructure. CarryPilot does not guarantee profit. Run paper mode first and use small sizes before any live deployment.

## What CarryPilot does

- Scans funding rates across multiple perpetual futures venues.
- Finds delta-neutral carry setups:
  - SHORT on the venue with the higher funding rate.
  - LONG on the venue with the lower funding rate.
- Estimates net profit after approximate fees and bid/ask spread costs.
- Sends Telegram signals with buttons:
  - `Approve` — open the pair.
  - `Skip` — ignore the signal.
  - `Details` — view balances and prices.
- Re-checks trade conditions before opening.
- Tracks open positions and statistics.
- Automatically closes positions based on risk rules.

## Supported venues

Current project structure:

- `extended` — trading mode.
- `nado` — trading mode.
- `01` / `zero_one` — trading mode.
- `variational` — read-only monitoring.
- `kraken` — configuration exists; actual usage depends on the exchange adapter implementation.

Venues with `read_only=True` are used only for monitoring and are not used for trade execution.

## Architecture

```text
main.py
├── exchanges/          # exchange adapters
├── core/scanner.py     # funding opportunity discovery
├── core/executor.py    # pair opening / closing
├── core/monitor.py     # scan loop + risk monitoring
├── core/position_tracker.py
├── core/paper_ledger.py
├── core/live_ledger.py
├── bot/telegram_bot.py # Telegram commands + approval flow
└── utils/              # calculations and logging
```

## Risk model

CarryPilot is designed as a semi-automated workflow, not as a fully autonomous trading bot.

### Before entry

- The signal must pass filters for:
  - minimum funding spread;
  - minimum estimated net profit;
  - maximum bid/ask spread;
  - minimum time to funding;
  - sanity cap for anomalous funding rates;
  - available balance on both venues;
  - no already-open position for the same symbol.
- After the user clicks `Approve`, the bot performs a re-check:
  - pings both venues;
  - verifies the funding spread is still valid;
  - checks that price has not drifted too far;
  - checks bid/ask spread limits;
  - verifies enough time remains before funding;
  - verifies sufficient balance.

### After entry

- Open positions are checked approximately every 30 seconds.
- Auto-close conditions:
  - one leg disappears or the position becomes unhealthy;
  - stop-loss: any leg loses more than 50% of margin;
  - time-based close: randomized close target around 4–5 hours, if more than 5 minutes remain before funding.
- In live mode, if one leg fails to open, the bot attempts to emergency-close the other leg.

## Main risks

- Execution risk: one leg may open while the other fails.
- Liquidity / slippage risk: the funding spread may not cover slippage.
- API risk: an exchange may return stale or incomplete data.
- Funding timing risk: the funding rate can change before the actual funding payment.
- Basis risk: prices can diverge across venues.
- Operational risk: Telegram, server, network, or exchange APIs can fail.
- Key risk: `.env` contains private keys and must never be committed.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/milanewgpt/carrypilot.git
cd carrypilot
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Example variables:

```env
# Extended exchange
EXTENDED_API_KEY=your_extended_api_key
EXTENDED_PRIVATE_KEY=0x...
EXTENDED_PUBLIC_KEY=0x...

# Nado DEX
NADO_PRIVATE_KEY=0x...
NADO_WALLET_ADDRESS=0x...

# 01.xyz
ZERO_ONE_PRIVATE_KEY=your_zero_one_private_key

# Telegram
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# Mode
PAPER_TRADING=true

# Bot settings
POSITION_MARGIN_USD=5
LEVERAGE=2
MIN_FUNDING_SPREAD=0.0003
MIN_NET_PROFIT=0.00015
MIN_MINUTES_TO_FUNDING=15
MAX_BID_ASK_SPREAD=0.0005
SCAN_INTERVAL=30
```

## Running

### Paper mode — recommended first run

```bash
PAPER_TRADING=true python main.py
```

Or set it in `.env`:

```env
PAPER_TRADING=true
```

Then run:

```bash
python main.py
```

### Live mode

Only after paper-mode validation:

```env
PAPER_TRADING=false
```

```bash
python main.py
```

## Docker

```bash
docker build -t carrypilot .
docker run --env-file .env carrypilot
```

## systemd

The repository includes an example service file:

```text
systemd/funding-arb-bot.service
```

Basic flow:

```bash
sudo cp systemd/funding-arb-bot.service /etc/systemd/system/funding-arb-bot.service
sudo systemctl daemon-reload
sudo systemctl enable funding-arb-bot
sudo systemctl start funding-arb-bot
sudo systemctl status funding-arb-bot
```

Check paths in the service file before use:

> If the bot is already deployed in `/home/gpt/funding-arb-bot`, the old server path can remain unchanged. Renaming the GitHub repository does not require changing the deployment directory.

```ini
User=gpt
WorkingDirectory=/home/gpt/funding-arb-bot
EnvironmentFile=/home/gpt/funding-arb-bot/.env
ExecStart=/usr/bin/python3 main.py
```

## Telegram commands

- `/status` — show open positions.
- `/opportunities` — scan the market manually.
- `/balances` — show exchange balances.
- `/close SYMBOL` — close one position.
- `/closeall` — close all positions.
- `/exchanges` — enable / disable venues for signals.
- `/pause` — pause new signals.
- `/resume` — resume new signals.
- `/settings` — show current settings.
- `/stats` — show paper/live trade statistics.
- `/log` — show paper trade history.

## Strategy settings

Main runtime parameters are set via `.env`:

- `POSITION_MARGIN_USD` — margin per side.
- `LEVERAGE` — leverage.
- `MIN_FUNDING_SPREAD` — minimum gross funding spread.
- `MIN_NET_PROFIT` — minimum estimated net profit after costs.
- `MIN_MINUTES_TO_FUNDING` — avoid entering too close to funding.
- `MAX_BID_ASK_SPREAD` — liquidity filter.
- `SCAN_INTERVAL` — scan interval in seconds.
- `PAPER_TRADING` — paper/live mode.

Additional YAML configuration is available at:

```text
config/settings.yaml
```

In the current version, primary runtime configuration is read from `.env`.

## Paper trading

Paper mode:

- does not send real orders;
- simulates fills using current mark prices;
- writes trades to `paper_trades.json`;
- helps validate:
  - signal quality;
  - opportunity frequency;
  - auto-close behavior;
  - Telegram approval flow;
  - PnL decomposition between price delta and estimated funding.

Recommended workflow:

1. Run with `PAPER_TRADING=true`.
2. Let the bot run for several days.
3. Check `/stats` and `/log`.
4. Tighten filters if there are too many signals or unstable PnL.
5. Move to live mode only with minimal margin.

## Security

- Never commit `.env`.
- Use separate wallets/API keys with limited balances.
- Do not keep large balances on experimental perp venues.
- For live mode, prefer a dedicated server/user and systemd service.
- After any code or configuration change, test in paper mode first.

## Development notes

Check imports and syntax:

```bash
python -m compileall .
```

Manual run:

```bash
python main.py
```

Logs are written to stdout via the project logger.

## Disclaimer

CarryPilot is intended for research and semi-automated execution. It is not financial advice and does not guarantee profitability. Funding arbitrage can only work if execution, liquidity, fees, API reliability, and operational risk are controlled.
