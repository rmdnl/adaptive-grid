# adaptive-grid

A conservative multi-symbol **Binance Spot** grid bot with strict indicator-based
entry/exit gates, executable grid-economics validation, a global 2% drawdown
kill switch, and a read-only dashboard.

**Dry-run is the default. Live trading is disabled by every default and
requires three explicit configuration gates.**

---

## Purpose

The bot waits for quiet, oversold market conditions, places a small grid of
post-only limit buy orders below the price, sells each filled buy one grid
step higher, and exits completely when conditions turn hostile. Capital
protection dominates every other concern:

- Trade **Binance Spot only** — no futures, margin, leverage, shorting,
  martingale, or aggressive averaging.
- Enter only under strict conditions; exit the moment conditions deteriorate.
- Never place a grid whose executable net profit cannot clear the minimums.
- Stop everything at a 2% global equity drawdown.

## Safety limitations (read first)

- **No profitability guarantee.** Nothing here promises profits. The economics
  gates only prevent *known-unprofitable* grids.
- **Testnet is the default environment.** Testnet prices and liquidity differ
  from production; signals computed on testnet data are for plumbing
  validation, not performance evidence.
- **Equity is PnL-based**, anchored at `START_EQUITY`:
  `equity = START_EQUITY + realized_pnl - fees + unrealized_pnl`. It is not a
  full account-balance reconciliation. The 2% drawdown kill therefore
  measures loss of the simulated capital base.
- **Kill states do not auto-reset.** A global kill or a symbol `STOPPED` by
  the lower-boundary gate stays until the operator intervenes (state database
  edit or fresh database).
- The 15m lower-boundary gate is **fail-closed**: missing or invalid 15m data
  blocks new orders for the affected symbol instead of guessing.
- Live-mode fill reconciliation is implemented but has not been exercised
  against production conditions; treat live mode as unproven.

## Architecture

```
adaptive-grid/
├── .env.example      # configuration template (.env is the ONLY config source)
├── config.py         # load + validate .env -> one immutable Config object
├── indicators.py     # deterministic ADX/RSI/BB %B/VO/Z-score/ATR (closed candles only)
├── strategy.py       # strict entry gate, exit gate, exit-priority, cooldown
├── grid.py           # grid construction + executable economics (quantization-aware)
├── risk.py           # order vetoes, 2% drawdown kill, 15m lower-boundary gate
├── exchange.py       # Binance Spot REST (testnet default), dry-run + live executors
├── state.py          # single SQLite database (state, orders, fills, PnL, kills)
├── bot.py            # runtime loop orchestrating the full cycle
├── dashboard.py      # read-only HTTP dashboard over the state database
└── tests/            # focused deterministic test suite (offline)
```

There is **no YAML configuration, no compatibility layers, and no duplicated
config sources**. `.env` is the single source of truth; every other module
receives the validated `Config` object and never reads the environment itself.

## Configuration

Copy `.env.example` to `.env` and fill it in. Startup fails with a clear error
listing every missing or invalid key — there are no silent fallbacks for
strategy values.

Key groups:

| Group | Keys |
|---|---|
| Environment & safety | `BINANCE_ENV`, `DRY_RUN`, `ALLOW_LIVE_EXECUTION` |
| Market scope | `PAIR_LIST` (e.g. `BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT`), `INDICATOR_TIMEFRAME` |
| Indicators | `ADX_PERIOD`, `RSI_PERIOD`, `BB_PERIOD`, `BB_STD`, `VO_FAST`, `VO_SLOW`, `ZSCORE_PERIOD`, `ATR_PERIOD` |
| Entry gate (ALL must hold) | `ENTRY_ADX_MAX`, `ENTRY_RSI_MAX`, `ENTRY_VOLUME_OSC_MIN`, `ENTRY_BB_PERCENT_B_MAX` |
| Exit gate (ANY triggers) | `EXIT_RSI_MIN`, `EXIT_ADX_MIN`, `EXIT_BB_PERCENT_B_MIN`, `EXIT_ZSCORE_ABS_MAX` |
| Grid economics | `GRID_STEP_ATR_MULTIPLIER`, `GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`, `MAKER_FEE`, `TAKER_FEE`, `SLIPPAGE_ESTIMATE` |
| Risk | `MAX_DRAWDOWN_PERCENT` (hard cap 2), `STOP_IF_BELOW_LOWER_PERCENT` (hard cap 2), `START_EQUITY`, `COOLDOWN_HOURS` |
| Credentials | `BINANCE_TESTNET_API_KEY/SECRET`, `BINANCE_LIVE_API_KEY/SECRET` |

Hard floors enforced by validation: `GRID_GROSS_MIN >= 0.005` (0.50%),
`MIN_NET_PROFIT_PER_GRID >= 0.002` (0.20%), `MAX_DRAWDOWN_PERCENT <= 2`,
`STOP_IF_BELOW_LOWER_PERCENT <= 2`.

### Live-trading gates

Live endpoints and live credentials are used **only** when **all three** hold:

1. `DRY_RUN=false`
2. `ALLOW_LIVE_EXECUTION=true`
3. `BINANCE_ENV=live`

Any other combination stays on testnet (or refuses to start — e.g. `live`
environment with `DRY_RUN=false` but the gate closed is rejected at startup).
`DRY_RUN=true` (the default) never submits an order to Binance.

## Strategy rules

**Entry** (a symbol may start a grid only when ALL hold, on CLOSED candles of
`INDICATOR_TIMEFRAME`):

- ADX(14) < 20
- RSI(14) < 35
- Volume Oscillator(5,10) > 0
- Bollinger %B(20,2) <= 0

Insufficient candle history means **NO TRADE** — never an exception, never a
fabricated value.

**Exit** (an active grid exits when ANY holds; exit has priority over entry):

- RSI(14) >= 70
- ADX(14) > 25
- Bollinger %B > 1
- abs(Z-Score(20)) > 2.5

Automatic exit: stop new orders → cancel all open orders → **verify** →
market-sell held inventory → **verify** → record exit reason, realized PnL and
fees → cooldown (`COOLDOWN_HOURS`, default 3h) → no automatic re-entry during
cooldown. Any failed verification is fail-closed: the symbol stops in `ERROR`.

After an automatic exit the symbol cannot start a new grid until the cooldown
expires. Cooldown survives process restart.

## Grid economics

- Grid step = `GRID_STEP_ATR_MULTIPLIER × ATR(14)` (default 1.0).
- Arithmetic grid for BTC/ETH/BNB, geometric for SOL (mapping in `config.py`).
- Buy prices are rounded **down** and sell prices **up** to the exchange tick
  size; quantities respect step size and minimum notional.
- The **executable** economics (after exchange quantization, with buy fee,
  sell fee and estimated slippage on both sides) are authoritative.
- A grid is **blocked** unless executable gross >= 0.50% and executable net
  >= 0.20% (`GRID_GROSS_MIN`, `MIN_NET_PROFIT_PER_GRID`). Bad grids are never
  widened or forced.

## Risk rules

- **Global drawdown kill switch: 2%** (`MAX_DRAWDOWN_PERCENT`, hard cap).
  Drawdown is measured against the high-water mark of
  `equity = START_EQUITY + realized - fees + unrealized`. At breach: all open
  orders across all symbols are cancelled and verified, new orders stop
  globally, the kill state is persisted, and **it never auto-clears**.
- **15m lower-boundary protection** (`STOP_IF_BELOW_LOWER_PERCENT`, hard cap
  2%): if the latest **CLOSED 15m candle close** (never an intrabar wick) is
  at most `lower × (1 - 2%)`, the symbol is stopped (orders cancelled,
  inventory liquidated, state `STOPPED`). Missing/invalid 15m data blocks new
  orders for that symbol (fail-closed) instead of triggering or guessing.
- The risk engine holds **veto authority over every order**: global kill or a
  risk-stopped symbol vetoes all placements.
- Orders use LIMIT_MAKER (post-only) where supported, carry unique client
  order ids, are persisted before submission, and are **never retried
  blindly**: after a network failure the order is reconciled by client id;
  an unknown final state raises a fail-closed condition and stops the symbol.

## State

A single SQLite database (`state.db` by default, path via `--db`) holds
global bot state, per-symbol state, cooldown, grid plan values, orders,
fills, fees, realized PnL, risk events and kill state. There are no
compatibility tables for any earlier architecture.

## Dashboard

```bash
python dashboard.py --db state.db --host 127.0.0.1 --port 8080
```

Read-only: it serves `GET /` (HTML) and `GET /api/state` (JSON), implements
no strategy logic, places/cancels nothing, exposes no credentials, and
refuses every write method with `405`. Global section: equity, reference
equity, drawdown, kill state/reason, open orders, realized PnL, fees, runtime
and database status. Per symbol: timeframe, last price, ADX, RSI, %B, VO,
Z-score, ATR, strategy state, entry status/blocker, exit status/reason,
cooldown, grid mode/step/count, gross & net per grid, inventory, open orders,
realized PnL, fees, risk status.

Symbol states are displayed verbatim from the database — never inferred:
`WAITING`, `ENTRY_BLOCKED`, `GRID_BLOCKED`, `ACTIVE`, `COOLDOWN`, `EXITING`,
`STOPPED`, `KILL_ACTIVE`, `ERROR`.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit .env
```

Python 3.10+ recommended. Runtime dependencies are minimal: `python-dotenv`
(plus `pytest` for the test suite).

## Running (dry-run)

```bash
python bot.py --once          # one cycle
python bot.py                 # loop (30s cycle), DRY_RUN=true by default
```

Startup logs the environment as `TESTNET` or `LIVE` (never credentials) and
refuses to start on any configuration problem.

### Testnet setup

1. Create API keys at <https://testnet.binance.vision/>.
2. Put them in `.env` under `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`.
3. Keep `BINANCE_ENV=testnet`. With `DRY_RUN=false` the bot submits real
   **testnet** orders (reconciliation, filters, fees all apply); with
   `DRY_RUN=true` (default) nothing is ever submitted.

## Testing

```bash
pytest -q
```

The suite (~140 tests) is deterministic and offline: configuration validation
and live gates, indicator math against hand-computed references, strict
entry/exit thresholds and exit priority, grid quantization and executable
economics, risk vetoes and kill persistence, state restart recovery,
dashboard read-only behavior, and bot-cycle integration (fills, exits,
cooldown, boundary stop, drawdown kill) against a stub market.

## Live mode — warning

Live trading is disabled by every default. Enabling it requires the three
explicit gates above and a deliberate operator decision on a machine where
`.env` contains live keys. **Do not enable live mode without independent
review.** The authors accept no liability for trading losses.
