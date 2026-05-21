# Funding Arb Bot

Telegram-бот для полуавтоматического арбитража funding rates между perpetual exchanges.

Проект сканирует разницу funding rates между биржами, фильтрует связки по net profit / ликвидности / времени до funding и отправляет сигнал в Telegram. Сделка открывается только после ручного approve через кнопку.

> ⚠️ Это high-risk trading infrastructure. Бот не гарантирует прибыль. Перед live-режимом обязательно гонять paper mode и малые размеры.

## Что делает бот

- Сканирует funding rates по нескольким perp-площадкам.
- Ищет delta-neutral связки:
  - SHORT на бирже с более высоким funding rate.
  - LONG на бирже с более низким funding rate.
- Считает примерный net profit после fees и bid/ask spread.
- Отправляет сигнал в Telegram с кнопками:
  - `Approve` — открыть пару.
  - `Skip` — пропустить.
  - `Details` — посмотреть балансы и цены.
- Перед открытием повторно проверяет условия сделки.
- Ведёт открытые позиции и статистику.
- Автоматически закрывает позиции по risk rules.

## Поддерживаемые площадки

Текущая структура проекта:

- `extended` — trading mode.
- `nado` — trading mode.
- `01` / `zero_one` — trading mode.
- `variational` — read-only monitoring.
- `kraken` — есть конфиг, но фактическое подключение зависит от реализации exchange adapter.

Биржа с `read_only=True` используется только для мониторинга и не участвует в открытии сделок.

## Архитектура

```text
main.py
├── exchanges/          # adapters бирж
├── core/scanner.py     # поиск funding opportunities
├── core/executor.py    # открытие / закрытие пар
├── core/monitor.py     # scan loop + risk monitoring
├── core/position_tracker.py
├── core/paper_ledger.py
├── core/live_ledger.py
├── bot/telegram_bot.py # Telegram commands + approve flow
└── utils/              # расчёты и логирование
```

## Risk model

Бот построен как semi-automated workflow, не как полностью автономный trading bot.

### Перед входом

- Сигнал проходит фильтры:
  - минимальный funding spread;
  - минимальный net profit;
  - максимальный bid/ask spread;
  - минимальное время до funding;
  - sanity cap по аномальным funding rates;
  - наличие баланса на обеих биржах;
  - отсутствие уже открытой позиции по символу.
- Перед approve бот делает re-check:
  - ping обеих бирж;
  - funding spread всё ещё актуален;
  - цена не ушла слишком далеко;
  - bid/ask spread в лимите;
  - достаточно времени до funding;
  - достаточно баланса.

### После входа

- Контроль открытых позиций каждые ~30 секунд.
- Auto-close conditions:
  - одна нога пропала / позиция стала unhealthy;
  - stop-loss: любая нога в убытке больше 50% margin;
  - time-based close: случайная цель закрытия примерно через 4–5 часов, если до funding больше 5 минут.
- Если в live mode одна нога не открылась, бот пытается аварийно закрыть вторую.

## Основные риски

- Execution risk: одна нога может открыться, вторая — нет.
- Liquidity / slippage risk: funding spread может не покрыть проскальзывание.
- API risk: биржа может вернуть stale / incomplete данные.
- Funding timing risk: ставка может измениться до фактического funding.
- Basis risk: цены на разных площадках могут разъехаться.
- Operational risk: Telegram, сервер, сеть или exchange API могут упасть.
- Key risk: `.env` содержит приватные ключи, его нельзя коммитить.

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/milanewgpt/funding-arb-bot.git
cd funding-arb-bot
```

### 2. Создать virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Пример переменных:

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

## Запуск

### Paper mode — рекомендуемый первый запуск

```bash
PAPER_TRADING=true python main.py
```

Или через `.env`:

```env
PAPER_TRADING=true
```

Затем:

```bash
python main.py
```

### Live mode

Только после проверки paper mode:

```env
PAPER_TRADING=false
```

```bash
python main.py
```

## Docker

```bash
docker build -t funding-arb-bot .
docker run --env-file .env funding-arb-bot
```

## systemd

В репозитории есть пример service file:

```text
systemd/funding-arb-bot.service
```

Базовый flow:

```bash
sudo cp systemd/funding-arb-bot.service /etc/systemd/system/funding-arb-bot.service
sudo systemctl daemon-reload
sudo systemctl enable funding-arb-bot
sudo systemctl start funding-arb-bot
sudo systemctl status funding-arb-bot
```

Перед использованием проверь пути внутри service file:

```ini
User=gpt
WorkingDirectory=/home/gpt/funding-arb-bot
EnvironmentFile=/home/gpt/funding-arb-bot/.env
ExecStart=/usr/bin/python3 main.py
```

## Telegram commands

- `/status` — открытые позиции.
- `/opportunities` — ручной scan рынка.
- `/balances` — балансы по биржам.
- `/close SYMBOL` — закрыть одну позицию.
- `/closeall` — закрыть все позиции.
- `/exchanges` — включить / отключить биржи для сигналов.
- `/pause` — остановить отправку новых сигналов.
- `/resume` — возобновить сигналы.
- `/settings` — текущие настройки.
- `/stats` — статистика paper/live сделок.
- `/log` — история paper trades.

## Настройки стратегии

Основные параметры задаются через `.env`:

- `POSITION_MARGIN_USD` — margin на одну сторону.
- `LEVERAGE` — плечо.
- `MIN_FUNDING_SPREAD` — минимальный gross funding spread.
- `MIN_NET_PROFIT` — минимальный net profit после примерных costs.
- `MIN_MINUTES_TO_FUNDING` — не входить слишком близко к funding.
- `MAX_BID_ASK_SPREAD` — фильтр ликвидности.
- `SCAN_INTERVAL` — интервал сканирования в секундах.
- `PAPER_TRADING` — paper/live режим.

Дополнительный YAML-конфиг лежит в:

```text
config/settings.yaml
```

На текущей версии основная runtime-конфигурация читается из `.env`.

## Paper trading

Paper mode:

- не отправляет реальные orders;
- симулирует fill по текущим mark prices;
- пишет сделки в `paper_trades.json`;
- позволяет проверить:
  - качество сигналов;
  - частоту opportunities;
  - поведение auto-close;
  - Telegram approve flow;
  - PnL decomposition между price delta и funding estimate.

Рекомендуемый workflow:

1. Запустить `PAPER_TRADING=true`.
2. Дать боту поработать несколько дней.
3. Проверить `/stats` и `/log`.
4. Ужесточить фильтры, если сигналов слишком много или PnL нестабилен.
5. Только потом переходить к live с минимальной маржой.

## Security

- Никогда не коммить `.env`.
- Используй отдельные wallets/API keys с лимитированным балансом.
- Не держи крупные суммы на экспериментальных perp-площадках.
- Для live лучше использовать отдельный сервер/user и systemd service.
- После любых изменений сначала запускать paper mode.

## Development notes

Проверка импорта / синтаксиса:

```bash
python -m compileall .
```

Ручной запуск:

```bash
python main.py
```

Логи выводятся в stdout через project logger.

## Disclaimer

Проект предназначен для research / semi-automated execution. Это не financial advice и не гарантия доходности. Funding arbitrage может быть прибыльным только при контроле execution, liquidity, fees, API reliability и operational risk.
