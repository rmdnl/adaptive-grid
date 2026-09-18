# Adaptive Risk-Controlled Grid Engine v3.2

This package is a full replacement of the previous v3.1 foundation.

## What changed

The replacement is deliberately **dry-run only**. It does not submit orders.

The main safety changes are:

- Risk decisions are now a real veto. A blocked condition prevents the order plan.
- Unknown Binance modes fail closed. There is no silent fallback from a typo to production.
- `Decimal` is used for grid price math.
- Grid profit is validated across every adjacent cell.
- Binance `exchangeInfo` is read and symbol filters are parsed.
- Price/quantity/notional quantization helpers are included.
- Indicators use Wilder-style RMA smoothing.
- RSI handles flat and one-sided markets without returning unnecessary NaN values.
- Volume ratio uses prior closed candles as its baseline.
- Incomplete 15m candles are removed before decisions.
- Auto range uses high/low quantiles instead of close-only quantiles.
- Range Quality includes width, ADX, ATR%, Bollinger width, volume, and current position.
- Fee retrieval is attempted through Binance account commission when credentials are available, with configured fallback rates otherwise.
- SQLite uses WAL, busy timeout, foreign keys, and persistent risk events.
- `.gitignore` protects `.env`, SQLite, and logs.
- Dependency versions are pinned to the tested package set.

## Safety status

This version is **not live-trading ready**.

There is intentionally no order placement, user-data WebSocket, inventory reservation, or exchange reconciliation in this replacement. Those need a separate implementation and test cycle.

Do not set `dry_run=false` in this version. The config validator fails closed on that state.

## Install

Python 3.10+ is required.

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

Linux / Armbian:

```bash
source .venv/bin/activate
```

Install:

```bash
pip install -r requirements.txt
```

Create local env:

```bash
cp .env.example .env
```

Populate API keys only when needed. Public market-data calls do not require keys.

Run:

```bash
python main.py
```

Run tests:

```bash
pytest -q
```

## Binance Testnet

Use the Binance Spot Test Network first. The config is already set to:

```yaml
environment:
  mode: testnet
  dry_run: true
```

The testnet endpoint is:

```text
https://testnet.binance.vision/api
```

The Spot Test Network supports `/api/*` endpoints and is periodically reset, so local test balances and test orders should not be treated as permanent state.

## Expected validation behavior

Example shape:

```text
Result:
  Risk decision : PASS
  Reason        : PASS
  Price         : 650.1234
  Range         : 620 -> 660
  Grid cells    : 10
  Net/grid      : 0.3487%
  Range quality : 78.50/100
  Fee source    : FALLBACK:...
  Execution     : DRY RUN, no order placement
```

An unsafe configuration should look like:

```text
Result:
  Risk decision : BLOCK
  Reason        : NET_PROFIT_BELOW_HARD_MIN | MARKET_FILTER_BLOCK:ADX
```

The program must never submit an order in this version.

## Design rules

- Spot only.
- No leverage.
- No futures.
- No margin.
- No martingale.
- No aggressive averaging.
- Net profit below 0.30% blocks the grid.
- 15m closed candles are the decision source.
- The risk engine is the veto layer.
- Exchange filters are treated as hard constraints.
- State storage is persistent, but Binance remains the source of truth for future reconciliation.

## Next implementation stage

The next stage should add, in this order:

1. Inventory manager.
2. User-data WebSocket.
3. REST reconciliation.
4. LIMIT_MAKER order engine.
5. Partial-fill handling.
6. Cancel / replace lifecycle.
7. Crash recovery.
8. Kill-switch execution.
9. Testnet integration tests.
10. Long dry-run soak test.

Only after those pass should live execution be considered.
