from dataclasses import dataclass
from typing import Any
import pandas as pd

try:
    from binance_sdk_spot.spot import Spot, ConfigurationRestAPI, SPOT_REST_API_PROD_URL
    from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
except ImportError:
    Spot = ConfigurationRestAPI = SPOT_REST_API_PROD_URL = KlinesIntervalEnum = None

INTERVAL_MAP = {
    "1m": "INTERVAL_1m", "3m": "INTERVAL_3m", "5m": "INTERVAL_5m",
    "15m": "INTERVAL_15m", "30m": "INTERVAL_30m", "1h": "INTERVAL_1h",
    "2h": "INTERVAL_2h", "4h": "INTERVAL_4h", "6h": "INTERVAL_6h",
    "8h": "INTERVAL_8h", "12h": "INTERVAL_12h", "1d": "INTERVAL_1d",
}

@dataclass
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame

def _base_path(mode: str) -> str:
    if mode == "testnet":
        return "https://testnet.binance.vision/api"
    if mode == "demo":
        return "https://demo-api.binance.com/api"
    return "https://api.binance.com/api"

def make_client(mode="testnet", api_key="", api_secret=""):
    if Spot is None:
        raise RuntimeError("Install binance-sdk-spot first")
    cfg = ConfigurationRestAPI(
        api_key=api_key,
        api_secret=api_secret,
        base_path=_base_path(mode),
    )
    return Spot(config_rest_api=cfg)

def fetch_klines(client, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported interval: {interval}")
    enum_name = INTERVAL_MAP[interval]
    enum_value = getattr(KlinesIntervalEnum, enum_name, None)
    if enum_value is None:
        try:
            enum_value = KlinesIntervalEnum[enum_name].value
        except Exception as exc:
            raise RuntimeError(f"SDK interval enum unavailable: {enum_name}") from exc
    if hasattr(enum_value, "value"):
        enum_value = enum_value.value

    response = client.rest_api.klines(
        symbol=symbol.upper(),
        interval=enum_value,
        limit=min(int(limit), 1000),
    )
    rows = response.data()
    if not rows:
        raise RuntimeError("Binance returned no klines")

    cols = ["open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades","taker_base",
            "taker_quote","ignore"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open","high","low","close","volume","quote_volume","taker_base","taker_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)
