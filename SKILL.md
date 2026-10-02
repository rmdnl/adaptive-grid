---
name: adaptive-grid-engineering
description: Engineering discipline for the adaptive-grid Binance Spot bot: architecture, implementation, debugging, testing, risk controls, exchange integration, state management, reconciliation, and code review.
---

# Adaptive Grid Engineering

Priority order:
1. Capital protection
2. Deterministic behavior
3. Correctness
4. Testability
5. Exchange-state reconciliation
6. Maintainability

## Hard invariants

Never weaken, remove, bypass, or silently change:

- Binance Spot only.
- No futures, margin, leverage, shorting, martingale, or aggressive averaging.
- DRY_RUN is the default.
- Live trading remains disabled unless explicitly authorized.
- Minimum net profit per completed grid: 0.30% (`0.003`).
- Default gross grid step: 0.60% (`0.006`).
- Conservative fee/slippage assumptions.
- Never place orders outside LOWER_PRICE / UPPER_PRICE.
- Risk Engine has final veto authority over every order.
- Equity drawdown kill switch: 2%.
- Lower-boundary 15m stop remains fail-closed.
- Kill state prevents new orders and survives restart.
- Never expose, hardcode, log, or commit secrets.
- Never require withdrawal permission.
- Never silently reset reference equity or risk state.

If a requested change conflicts with an invariant, stop that change and document the conflict.

## Autonomous workflow

READ -> UNDERSTAND -> PLAN -> IMPLEMENT -> TARGETED TEST -> FULL TEST -> REVIEW DIFF -> SECURITY CHECK -> COMMIT -> CONTINUE

Before editing:
- Check git status.
- Preserve unrelated user changes.
- Read relevant implementation and tests.

After editing:
- Run targeted tests.
- Run `pytest -q`.
- Inspect git diff.
- Check for secrets and accidental files.
- Update documentation when behavior changes.
- Commit only coherent, tested changes.

Never declare success while required tests fail.

## Testing discipline

Never delete, weaken, skip, or xfail tests merely to obtain a passing suite.

For execution work, test:
- duplicate-order prevention
- idempotent retries
- partial fills
- rejected/cancelled orders
- unknown order states
- stale state
- restart recovery
- exchange/network failures
- symbol filters
- tick/step sizes
- min/max notional
- percent-price filters
- fee calculation
- risk vetoes
- kill switch
- cancel-on-kill
- prevention of new orders while killed

## Binance discipline

Treat exchange responses as external state.

Account for:
- PRICE_FILTER
- LOT_SIZE
- MARKET_LOT_SIZE
- MIN_NOTIONAL
- NOTIONAL
- PERCENT_PRICE
- PERCENT_PRICE_BY_SIDE
- relevant order-count limits
- actual commission components
- partial fills
- cancellation races
- network retries
- stale state

Never assume submission means fill, or that a cancel request means cancellation succeeded. Reconcile when correctness depends on exchange state.

## Risk Engine

The Risk Engine is a veto layer. No execution component may bypass it.

Protect against:
- price outside configured range
- range break
- drawdown limit
- lower-boundary stop
- stale market data
- invalid grid
- insufficient net grid profit
- invalid symbol filters
- duplicate/open-order conflicts
- kill state
- reconciliation uncertainty

Fail closed when a safe state cannot be established.

## Paper reference equity

Reference equity resets must never be silent.

A reset must be:
- explicit
- operator-triggered
- logged
- persisted
- tested
- auditable

Record previous value, new value, timestamp, and context/reason when available. Automatic resets are prohibited.

## Kill switch and cancel-on-kill

When kill state activates:
- prevent new orders immediately
- persist the kill state
- attempt to cancel relevant open orders
- never treat cancellation failure as success
- handle network/exchange errors
- handle already-filled/already-cancelled orders
- retry safely where appropriate
- reconcile exchange state afterward

A failed cancellation must never cause trading to resume.

## State and reconciliation

Design for:
- process crash
- restart
- network interruption
- websocket disconnect
- missed/duplicate/delayed events
- partial fills
- manual exchange-side changes

Reconciliation must be deterministic and repeatable. Never create duplicate orders because local state is stale.

## Configuration

Fail closed. Validate:
- price range
- grid step
- levels
- fees
- slippage
- minimum net profit
- drawdown limits
- symbol rules
- dry-run/live mode

Never silently clamp dangerous values unless explicitly documented and tested.

## Git

Prefer a dedicated branch such as `hermes/autonomous`.

Do not:
- push to `main`
- force-push
- rewrite history
- run destructive resets
- overwrite unrelated changes

Never commit `.env`, credentials, API keys, tokens, databases, logs, caches, `.venv`, or unintended generated artifacts.

## Architecture

Prefer:
Market Data -> Market Analyzer -> Range Engine -> Grid Engine -> Risk Engine -> Order Engine -> Fill/Inventory -> Reconciliation -> Storage

A future dashboard is an observability layer, not a trading-control layer and not a dependency of trading/risk logic.

## Feature priority

1. Correctness bugs
2. Risk-control gaps
3. Order lifecycle
4. Fill/inventory correctness
5. Reconciliation
6. Restart/crash recovery
7. Exchange event handling
8. Operational resilience
9. Tests
10. Documentation
11. Dashboard/UI

Do not add speculative trading features.

## Definition of done

A task is done only when implementation, relevant tests, full test suite, diff review, secret check, and documentation are complete.

Never claim guaranteed profitability or risk-free operation.
Never enable live trading autonomously.
