from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


try:
    from binance_sdk_spot.spot import (
        Spot,
        ConfigurationRestAPI,
        SPOT_REST_API_PROD_URL,
    )
    from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
except ImportError:  # Lazy failure for unit tests without the SDK.
    Spot = ConfigurationRestAPI = SPOT_REST_API_PROD_URL = None
    KlinesIntervalEnum = None


INTERVAL_MAP = {
    "1m": "INTERVAL_1m",
    "3m": "INTERVAL_3m",
    "5m": "INTERVAL_5m",
    "15m": "INTERVAL_15m",
    "30m": "INTERVAL_30m",
    "1h": "INTERVAL_1h",
    "2h": "INTERVAL_2h",
    "4h": "INTERVAL_4h",
    "6h": "INTERVAL_6h",
    "8h": "INTERVAL_8h",
    "12h": "INTERVAL_12h",
    "1d": "INTERVAL_1d",
}


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame


def _base_path(mode: str) -> str:
    if mode == "testnet":
        return "https://testnet.binance.vision/api"
    if mode == "live":
        return SPOT_REST_API_PROD_URL or "https://api.binance.com/api"
    raise ValueError(f"Unknown Binance mode: {mode}")


def make_client(mode: str, api_key: str = "", api_secret: str = ""):
    if Spot is None or ConfigurationRestAPI is None:
        raise RuntimeError("Install binance-sdk-spot==11.3.0")
    if mode not in {"testnet", "live"}:
        raise ValueError(f"Unsupported mode: {mode}")

    cfg = ConfigurationRestAPI(
        api_key=api_key or "",
        api_secret=api_secret or "",
        base_path=_base_path(mode),
        timeout=5000,
        retries=3,
        backoff=1000,
        keep_alive=True,
        compression=True,
    )
    return Spot(config_rest_api=cfg)


def _model_to_plain(value: Any):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _model_to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_to_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {
            k: _model_to_plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return value


def _enum_value(name: str):
    if KlinesIntervalEnum is None:
        raise RuntimeError("Binance Spot SDK is not installed")
    member = getattr(KlinesIntervalEnum, name, None)
    if member is not None:
        return getattr(member, "value", member)
    try:
        return KlinesIntervalEnum[name].value
    except Exception as exc:
        raise RuntimeError(f"SDK interval enum unavailable: {name}") from exc


def fetch_klines(
    client,
    symbol: str,
    interval: str = "15m",
    limit: int = 200,
    drop_incomplete: bool = True,
) -> pd.DataFrame:
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported interval: {interval}")

    response = client.rest_api.klines(
        symbol=symbol.upper(),
        interval=_enum_value(INTERVAL_MAP[interval]),
        limit=min(int(limit), 1000),
    )
    rows = response.data()
    rows = _model_to_plain(rows)

    if not rows:
        raise RuntimeError("Binance returned no klines")

    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base",
        "taker_quote", "ignore",
    ]

    normalized = []
    for row in rows:
        if isinstance(row, dict):
            row = list(row.values())
        normalized.append(list(row))

    df = pd.DataFrame(normalized, columns=columns)

    numeric = [
        "open", "high", "low", "close", "volume",
        "quote_volume", "taker_base", "taker_quote",
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

    if drop_incomplete:
        now = datetime.now(timezone.utc)
        df = df[df["close_time"] <= pd.Timestamp(now)].reset_index(drop=True)

    if len(df) < 50:
        raise RuntimeError("Not enough closed klines after removing incomplete candle")

    return df


def fetch_symbol_info(client, symbol: str) -> dict:
    response = client.rest_api.exchange_info(symbol=symbol.upper())
    payload = _model_to_plain(response.data())
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    if not symbols:
        raise RuntimeError(f"No exchangeInfo for {symbol}")
    info = symbols[0]
    if str(info.get("status", "")).upper() != "TRADING":
        raise RuntimeError(f"Symbol {symbol} is not TRADING")
    permissions = {p for p in info.get("permissions", []) if isinstance(p, str)}
    if permissions and "SPOT" not in permissions:
        raise RuntimeError(f"Symbol {symbol} does not expose SPOT permission")
    return info


def fetch_account_commission(client, symbol: str) -> tuple[dict | None, str]:
    try:
        response = client.rest_api.account_commission(symbol=symbol.upper())
        return _model_to_plain(response.data()), "ACCOUNT_COMMISSION"
    except Exception as exc:
        return None, f"FALLBACK:{type(exc).__name__}"
