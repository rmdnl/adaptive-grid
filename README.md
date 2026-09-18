# Adaptive Risk-Controlled Grid Engine v3.2.1

Safety foundation for a Binance Spot grid bot.

This release is intentionally **dry-run only**. It does not submit orders.

## Fixes in v3.2.1

- Fixed invalid-grid logging crash when grid validation is unavailable.
- Separated strict order-range gating from the wider ±buffer breakout kill switch.
- Fee handling now parses standard, special, and tax commission components conservatively.
- Exchange-symbol parsing now preserves both `NOTIONAL` and `MIN_NOTIONAL` constraints.
- Added `PERCENT_PRICE` / `PERCENT_PRICE_BY_SIDE` validation helpers.
- Added `MARKET_LOT_SIZE` parsing.
- Added exchange order-count filter parsing.
- Added focused regression tests for the above safety cases.

## Safety rules

- Spot only.
- No futures, margin, leverage, martingale, or aggressive averaging.
- Net grid profit below 0.30% blocks the grid.
- 15m closed candles are the decision source.
- Risk layer is a veto.
- No order is allowed when price is outside the configured lower/upper range.
- A wider ±buffer is used only for the range-break kill condition.
- Drawdown at or above 2% blocks.
- This version contains no order placement.

## Install

Python 3.10+.

```bash
python -m venv .venv
```

Linux / Armbian:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest -q
python main.py
```

## Binance testnet

The default configuration is:

```yaml
environment:
  mode: testnet
  dry_run: true
  allow_live_execution: false
```

No live execution is implemented in this release.
