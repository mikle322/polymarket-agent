# Polymarket Hedge Bot MVP

Read-only MVP agent for Polymarket touch markets hedged with BTC futures.

The goal is not auto-trading yet. The goal is to find candidate hedge setups, calculate sizing, rank risk/reward, and produce a clear action plan.

## What it does

- estimates touch probability from spot, strike, IV, and deadline;
- estimates fair NO probability;
- calculates true edge after buffers;
- recommends futures hedge direction and size;
- estimates simple worst-case after futures stop loss;
- ranks multiple candidate markets through `scout`;
- checks whether the Polymarket CLOB has enough ask-side liquidity for the intended stake;
- calculates NO VWAP, filled shares, worst filled price, and orderbook slippage;
- recommends futures notional, isolated leverage, and estimated margin;
- accounts for PM fees, futures entry/exit fees, and expected funding;
- reports clean net PnL scenarios after fees and funding;
- monitors an open PM position after a futures hedge loss;
- calculates partial exit size after a broken hedge;
- returns `ENTER`, `WATCH`, or `SKIP`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Telegram bot

Create a Telegram bot through `@BotFather`, copy the token, then create `.env` from `.env.example`:

```powershell
copy .env.example .env
```

Set:

```text
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_CHAT_ID=
```

`TELEGRAM_ALLOWED_CHAT_ID` is optional, but recommended after you know your chat id.

Run the bot:

```powershell
python -m polymarket_hedge_bot.telegram_bot
```

Telegram commands:

```text
/help
/ping
/menu
/scout --candidates examples/candidates.json --max-loss 200 --top 3
/analyze --slug test --strike 80000 --direction up --stake 200 --deadline 2026-05-01 --btc-price 77000 --iv 0.30 --no-price 0.57
/monitor --pm-cost 287.35 --pm-current-value 394.85 --pm-shares 509.5 --futures-realized-pnl -200.58 --max-loss 300
/pm_liquidity --slug market-slug-here --outcome No --stake 200
/status
/last_skips
/review_skips
/journal
/close trade_id --pnl 42.5 --note "manual result"
```

`/menu` opens the button UI:

```text
Bot -> status, ping, restart/stop instructions
Scanner -> scanner status and skipped opportunities
Skipped opportunities -> latest, review now, full loss, near zero, max plus, pending
Journal -> journal summary and close-trade help
```

`/status` shows the latest 24/7 scanner heartbeat: last scan time, source, scanned/matched/sent counts, IV/BTC inputs, filters, and the latest error if one happened.

`/last_skips` shows recently skipped or filtered-out opportunities, including reason, NO wins probability, edge, reward/risk, worst-case, and hypothetical PnL if NO wins or if touch happens.

`/review_skips` checks skipped opportunities whose deadlines have passed. If Polymarket has closed the market, the bot records whether the skipped setup would have been profitable. The 24/7 scanner also runs this review automatically and sends a Telegram summary when new skipped results are reviewed.

After `/analyze` and `/scout`, Telegram shows inline buttons:

```text
Зайшов
Зайшов #1
Зайшов #2
```

Pressing a button records the selected setup into `data/trade_journal.jsonl`. After closing the real trade, record the result:

```text
/close trade_id --pnl -18.40
```

Local dry-run without Telegram:

```powershell
python -m polymarket_hedge_bot.telegram_bot --dry-run "/ping"
```

## 24/7 scanner

Recommended one-process production runner:

```powershell
python -m polymarket_hedge_bot.bot_runner `
  --live-polymarket `
  --stake 200 `
  --live-pages 5 `
  --live-limit 100 `
  --live-min-liquidity 500 `
  --live-orderbook `
  --interval 15 `
  --http-timeout 4 `
  --max-workers 10
```

This starts both parts in one process:

```text
Telegram commands/buttons thread + 24/7 scanner loop
```

Use `--dry-run --once --no-telegram-polling` for a local test without Telegram:

```powershell
python -m polymarket_hedge_bot.bot_runner `
  --candidates examples/candidates.json `
  --once `
  --dry-run `
  --no-telegram-polling
```

Run one test scan without Telegram:

```powershell
python -m polymarket_hedge_bot.scanner `
  --candidates examples/candidates.json `
  --once `
  --dry-run
```

Run continuous scanner with Telegram alerts:

```powershell
python -m polymarket_hedge_bot.scanner `
  --candidates examples/candidates.json `
  --interval 60 `
  --min-decision WATCH `
  --min-score 60 `
  --min-edge 0.10 `
  --min-positive-probability 0.60 `
  --min-hours-to-deadline 6 `
  --min-no-price 0.05 `
  --max-no-price 0.90 `
  --cooldown-min 30
```

The scanner sends only filtered opportunities and avoids duplicate alerts during the cooldown window. The deadline and NO-price pre-filters remove noisy late-stage markets before hedge analysis, so skipped opportunities stay cleaner too.

### Faster market data mode

For quicker discovery, the scanner now fetches Polymarket pages and CLOB orderbooks in parallel.

```text
--interval       how often the scanner runs, default 60s
--http-timeout   timeout for each public API request, default 5s
--max-workers    parallel workers for Polymarket pages/orderbooks, default 8
--min-hours-to-deadline  ignore markets too close to resolution, default 6h
--min-no-price / --max-no-price  keep NO price in a sane range, default 0.05-0.90
--radar-*        softer observation filters for /radar, enabled by default
```

The scanner keeps strict signal filters and a softer Radar at the same time. Use `/radar` or the Scanner -> Radar button in Telegram to see interesting setups that are not clean enough for a normal alert yet.

Recommended VPS fast mode:

```bash
.venv/bin/python -m polymarket_hedge_bot.bot_runner \
  --live-polymarket \
  --stake 200 \
  --live-pages 5 \
  --live-limit 100 \
  --live-min-liquidity 500 \
  --live-orderbook \
  --interval 15 \
  --http-timeout 4 \
  --max-workers 10 \
  --min-hours-to-deadline 6 \
  --min-no-price 0.05 \
  --max-no-price 0.90
```

This is still polling. The lowest-latency upgrade is WebSocket streaming:

```text
Polymarket CLOB market WebSocket -> best_bid_ask / price_change / trades
Binance USD-M WebSocket -> BTCUSDT markPrice@1s / bookTicker
Deribit/OKX fallback -> IV / mark price / funding backup
```

The bot keeps REST as backup because WebSocket connections can disconnect and need automatic reconnect.

Current scanner loop:

```text
load candidates -> evaluate edge/risk/liquidity -> filter best setups -> send Telegram alert -> add "Зайшов" button -> remember alert cooldown
```

For true 24/7 work, run this process on a VPS or keep the PC awake. The next upgrade is replacing `examples/candidates.json` with live Polymarket market discovery.

## VPS 24/7 deploy

Recommended VPS: Ubuntu 22.04/24.04, 1 CPU, 1 GB RAM is enough for the current read-only scanner.

On the VPS:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
sudo adduser --disabled-password --gecos "" polymarket
sudo mkdir -p /opt/polymarket-agent
sudo chown polymarket:polymarket /opt/polymarket-agent
```

Copy this project to `/opt/polymarket-agent`, then:

```bash
sudo chown -R polymarket:polymarket /opt/polymarket-agent
cd /opt/polymarket-agent
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

Set in `.env`:

```text
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_ALLOWED_CHAT_ID=your_chat_id_here
```

Test once:

```bash
.venv/bin/python -m polymarket_hedge_bot.bot_runner --live-polymarket --stake 200 --live-pages 3 --live-limit 100 --live-min-liquidity 500 --live-orderbook --once --dry-run --no-telegram-polling
```

Install systemd service:

```bash
sudo cp deploy/polymarket-bot.service.example /etc/systemd/system/polymarket-bot.service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot
```

Check status and logs:

```bash
sudo systemctl status polymarket-bot
sudo journalctl -u polymarket-bot -f
```

Restart after config changes:

```bash
sudo systemctl restart polymarket-bot
```

## GitHub version control and auto deploy

Initialize git locally:

```powershell
git init
git add .
git commit -m "Initial Polymarket hedge bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Never commit `.env` or `data/`. They are ignored by `.gitignore`.

Auto deploy is configured in:

```text
.github/workflows/deploy-vps.yml
```

Add these GitHub repository secrets:

```text
VPS_HOST      your server IP
VPS_USER      root
VPS_PORT      22
VPS_SSH_KEY   private deploy SSH key
```

The workflow runs on every push to `main`:

```text
push to GitHub -> upload code to /opt/polymarket-agent -> install requirements -> restart polymarket-bot
```

Recommended deploy flow:

```powershell
git add .
git commit -m "Describe change"
git push
```

Then watch VPS logs:

```bash
sudo journalctl -u polymarket-bot -f
```

## Live Polymarket discovery

Find live BTC touch/reach markets from Polymarket Gamma API:

```powershell
python -m polymarket_hedge_bot.live_discovery `
  --btc-price 77000 `
  --iv 0.45 `
  --stake 200 `
  --pages 3 `
  --limit 100 `
  --min-liquidity 500 `
  --output data/live_candidates.json
```

Run the scanner directly from live Polymarket discovery:

```powershell
python -m polymarket_hedge_bot.scanner `
  --live-polymarket `
  --btc-price 77000 `
  --iv 0.45 `
  --stake 200 `
  --live-pages 3 `
  --live-limit 100 `
  --live-min-liquidity 500 `
  --live-orderbook `
  --interval 60
```

For now, `btc-price` and `iv` are still manual inputs. The next step is Binance/OKX price + funding and Deribit IV, so the live scanner no longer needs those parameters by hand.

## Binance public data

Test Binance USD-M futures public market data:

```powershell
python -m polymarket_hedge_bot.binance_market --symbol BTCUSDT
```

If Binance Futures returns HTTP 451, the command automatically falls back to OKX public swap data:

```powershell
python -m polymarket_hedge_bot.binance_market --symbol BTCUSDT --okx-inst-id BTC-USDT-SWAP
```

This uses public endpoints only:

```text
BTCUSDT last price
mark price
index price
last funding rate
best bid/ask
```

The live scanner can now auto-fill BTC mark price and funding rate from Binance:
If Binance is unavailable, it falls back to OKX.

```powershell
python -m polymarket_hedge_bot.scanner `
  --live-polymarket `
  --iv 0.45 `
  --stake 200 `
  --okx-inst-id BTC-USDT-SWAP `
  --live-pages 3 `
  --live-limit 100 `
  --live-min-liquidity 500 `
  --live-orderbook `
  --interval 60
```

`--iv` is still manual until Deribit IV is connected. `--btc-price` and `--funding-rate` are now optional for scanner mode.

## Deribit IV

Test public Deribit BTC volatility input:

```powershell
python -m polymarket_hedge_bot.deribit_iv
```

The live scanner can now auto-fill IV from Deribit when `--iv` is omitted:

```powershell
python -m polymarket_hedge_bot.scanner `
  --live-polymarket `
  --stake 200 `
  --live-pages 3 `
  --live-limit 100 `
  --live-min-liquidity 500 `
  --live-orderbook `
  --once `
  --dry-run
```

Current live data fallback chain:

```text
BTC price/funding: Binance -> OKX fallback
IV input: Deribit DVOL
Polymarket markets/orderbooks: Polymarket Gamma + CLOB
```

## Scout multiple candidates

```powershell
python -m polymarket_hedge_bot.cli scout `
  --candidates examples/candidates.json `
  --max-loss 200 `
  --max-futures-margin 2500 `
  --top 10
```

With live CLOB liquidity checks, add `no_token_id` to each candidate and run:

```powershell
python -m polymarket_hedge_bot.cli scout `
  --candidates examples/candidates.json `
  --live-orderbook `
  --max-slippage 0.03
```

## Check live Polymarket liquidity

Use a market slug and the bot will find the outcome token through the Gamma API, then read the CLOB orderbook:

```powershell
python -m polymarket_hedge_bot.cli pm-liquidity `
  --slug market-slug-here `
  --outcome No `
  --stake 200 `
  --max-slippage 0.03
```

Or use a known CLOB token id directly:

```powershell
python -m polymarket_hedge_bot.cli pm-liquidity `
  --token-id TOKEN_ID_HERE `
  --stake 200
```

The scout output is the first version of the agent loop:

```text
scan candidates -> calculate probability -> calculate edge -> size PM -> size futures -> risk check -> rank -> action plan
```

Fee and funding inputs:

```text
--pm-fee-rate           fee on Polymarket stake, default 0
--futures-fee-rate      futures fee per side, default 0.0005
--funding-rate          expected funding rate per period, signed
--funding-periods       expected number of funding periods
```

Funding sign:

```text
positive funding: LONG pays, SHORT receives
negative funding: LONG receives, SHORT pays
```

## Monitor an open position

```powershell
python -m polymarket_hedge_bot.cli monitor `
  --pm-cost 287.35 `
  --pm-current-value 394.85 `
  --pm-shares 509.5 `
  --futures-realized-pnl -200.58 `
  --max-loss 200
```

## Roadmap

### V1: Read-only decision agent

- scan manually supplied candidates;
- calculate fair touch probability;
- calculate real edge after buffers;
- size Polymarket NO;
- size Binance/OKX futures hedge;
- recommend isolated leverage and margin;
- produce `ENTER`, `WATCH`, or `SKIP`;
- produce post-SL action plan.

### V2: Live market scanner

- read Polymarket markets from Gamma API;
- detect BTC touch/reach markets;
- read CLOB orderbooks for YES/NO liquidity;
- calculate VWAP for the intended PM stake;
- read BTC price and futures orderbook from Binance/OKX;
- use Deribit IV for probability inputs;
- rank opportunities continuously.

### V3: Paper trading agent

- simulate entries, hedge TP/SL, partial exits, and full exits;
- log every skipped and entered setup;
- calculate expectancy by market type, distance, IV, spread, and time-to-expiry.

### V4: Manual execution assistant

- output exact human actions:
  `Buy NO up to price X`, `Open LONG/SHORT BTCUSDT size Y`, `TP`, `SL`, `post-SL action`.

### V5: Limited automation

- alerts, journal, kill switch, daily loss limit;
- only then consider automatic order placement.

## Example

```powershell
python -m polymarket_hedge_bot.cli analyze `
  --slug will-bitcoin-reach-80000-in-april `
  --strike 80000 `
  --direction up `
  --stake 200 `
  --deadline 2026-05-01 `
  --btc-price 77000 `
  --iv 0.55 `
  --no-price 0.57 `
  --futures-fee-rate 0.0005 `
  --funding-rate 0.0001 `
  --funding-periods 3
```

## Important

This is a calculator, not financial advice and not a trading bot. It intentionally says `SKIP` often.
