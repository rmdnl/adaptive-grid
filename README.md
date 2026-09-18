# Adaptive Risk-Controlled Grid Engine v3

Spot-only Binance grid bot architecture.

Core design:
- Fixed-range geometric grid, 0.60% baseline step.
- AUTO range with MANUAL override.
- Expected net-profit gate.
- Market-regime filter.
- Range quality score.
- Inventory-aware order sizing.
- Breakout/range-break protection.
- Spread/liquidity guard hooks.
- Equity drawdown kill switch at 2%.
- Reconciliation-first restart behavior.
- SQLite ledger and CSV trade log.
- Testnet + dry-run are the defaults.

The official Binance Python Spot SDK is used for REST integration. The official
Binance connector currently documents Python 3.10+ and the `binance-sdk-spot`
package. See:
https://github.com/binance/binance-connector-python
and the official Spot API documentation:
https://github.com/binance/binance-spot-api-docs

IMPORTANT:
This package is an engineering baseline, not a promise of profitability.
Run tests, dry-run, and Spot Testnet validation before any live deployment.
