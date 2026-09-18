# Adaptive Risk-Controlled Grid Engine v3.1

This is the cleaned foundation for the Binance Spot grid project.

## What is fixed in v3.1

- Real Binance kline retrieval through the official `binance-sdk-spot`.
- Testnet is the default.
- AUTO range now uses real candle data, not dummy prices.
- MANUAL range remains available.
- Range Quality Score now combines range width, ADX, ATR%, Bollinger width, and volume ratio.
- Real market filter gate for sideways conditions.
- Fixed geometric grid at 0.60%.
- Net-profit gate with 0.30% hard minimum.
- Corrected profit test.
- SQLite schema foundation.
- Explicitly keeps live order execution disabled in this foundation release.

## Run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/Armbian: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
pytest -q
```

The Binance Spot SDK officially supports Python 3.10+ and the `binance-sdk-spot` package. Binance's official docs list `/api/v3/klines`, exchange information, and Spot Testnet endpoints. Testnet supports Spot `/api/*` endpoints and uses `https://testnet.binance.vision/api`. 

IMPORTANT: v3.1 is deliberately NOT live-trading-ready. Order placement, user-data WebSocket, reconciliation, symbol-filter quantization, inventory engine, and kill-switch execution must be completed and tested before live funds are enabled.

Official references:
- https://github.com/binance/binance-connector-python
- https://github.com/binance/binance-spot-api-docs
