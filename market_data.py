from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any

import pandas as pd

try:
    from binance_sdk_spot.spot import Spot, ConfigurationRestAPI, SPOT_REST_API_PROD_URL
    from binance_sdk_spot.rest_api.models import KlinesIntervalEnum
except ImportError:
    Spot = ConfigurationRestAPI = SPOT_REST_API_PROD_URL = None
    KlinesIntervalEnum = None

INTERVAL_MAP = {
    "1m":"INTERVAL_1m","3m":"INTERVAL_3m","5m":"INTERVAL_5m","15m":"INTERVAL_15m",
    "30m":"INTERVAL_30m","1h":"INTERVAL_1h","2h":"INTERVAL_2h","4h":"INTERVAL_4h",
    "6h":"INTERVAL_6h","8h":"INTERVAL_8h","12h":"INTERVAL_12h","1d":"INTERVAL_1d",
}
MAX_TICKER_AGE_SECONDS = 10


class MarketDataError(RuntimeError):
    """Raised when Binance market data cannot be safely used."""


class AccountDataError(MarketDataError):
    """Raised when read-only Binance account data cannot be safely used."""


class AccountRequestError(AccountDataError):
    """Raised when Binance rejects or fails an account-information request."""


class AccountValidationError(AccountDataError):
    """Raised when Binance account information is malformed or unsafe."""


@dataclass(frozen=True)
class TickerSnapshot:
    symbol: str
    price: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class AccountSnapshot:
    base_asset: str
    base_free: Decimal
    base_locked: Decimal
    quote_asset: str
    quote_free: Decimal
    quote_locked: Decimal
    fetched_at: datetime

    @property
    def base_total(self) -> Decimal:
        return self.base_free + self.base_locked

    @property
    def quote_total(self) -> Decimal:
        return self.quote_free + self.quote_locked


@dataclass(frozen=True)
class AccountRiskState:
    current_equity: Decimal
    reference_equity: Decimal
    drawdown_pct: Decimal
    base_inventory: Decimal
    inventory_pct: Decimal

@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame

def _base_path(mode):
    if mode == "testnet":
        return "https://testnet.binance.vision/api"
    if mode == "live":
        return SPOT_REST_API_PROD_URL or "https://api.binance.com/api"
    raise ValueError(f"Unknown Binance mode: {mode}")

def make_client(mode, api_key="", api_secret=""):
    if Spot is None or ConfigurationRestAPI is None:
        raise RuntimeError("Install binance-sdk-spot==11.3.0")
    cfg = ConfigurationRestAPI(
        api_key=api_key or "", api_secret=api_secret or "",
        base_path=_base_path(mode), timeout=5000, retries=3, backoff=1000,
        keep_alive=True, compression=True,
    )
    return Spot(config_rest_api=cfg)

def _model_to_plain(value: Any):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {k:_model_to_plain(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_to_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k:_model_to_plain(v) for k,v in vars(value).items() if not k.startswith("_")}
    return value

def _enum_value(name):
    if KlinesIntervalEnum is None:
        raise RuntimeError("Binance Spot SDK is not installed")
    member = getattr(KlinesIntervalEnum, name, None)
    if member is not None:
        return getattr(member, "value", member)
    return KlinesIntervalEnum[name].value


def fetch_ticker_price(client, symbol) -> TickerSnapshot:
    """Fetch the current Spot ticker price for risk and execution gates.

    The Binance ticker response has no market-data timestamp, so ``fetched_at``
    deliberately records the local UTC time at which this request completed.
    """
    normalized_symbol = str(symbol).upper()
    try:
        response = client.rest_api.ticker_price(symbol=normalized_symbol)
        payload = _model_to_plain(response.data())
    except Exception as exc:
        raise MarketDataError(
            f"Binance ticker price request failed for {normalized_symbol}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise MarketDataError(
            f"Malformed Binance ticker price response for {normalized_symbol}: expected object"
        )

    returned_symbol = payload.get("symbol")
    if returned_symbol is not None and str(returned_symbol).upper() != normalized_symbol:
        raise MarketDataError(
            f"Malformed Binance ticker price response for {normalized_symbol}: "
            f"received symbol {returned_symbol!r}"
        )

    raw_price = payload.get("price")
    if raw_price is None or isinstance(raw_price, bool):
        raise MarketDataError(
            f"Malformed Binance ticker price response for {normalized_symbol}: missing price"
        )
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketDataError(
            f"Invalid Binance ticker price for {normalized_symbol}: {raw_price!r}"
        ) from exc
    if not price.is_finite() or price <= 0:
        raise MarketDataError(
            f"Invalid Binance ticker price for {normalized_symbol}: {raw_price!r}"
        )

    return TickerSnapshot(
        symbol=normalized_symbol,
        price=price,
        fetched_at=datetime.now(timezone.utc),
    )


def is_ticker_fresh(snapshot: TickerSnapshot, max_age_seconds=MAX_TICKER_AGE_SECONDS) -> bool:
    """Return whether a locally timestamped ticker is still safe to use."""
    try:
        max_age = Decimal(str(max_age_seconds))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("max_age_seconds must be a non-negative number") from exc
    if not max_age.is_finite() or max_age < 0:
        raise ValueError("max_age_seconds must be a non-negative number")

    fetched_at = getattr(snapshot, "fetched_at", None)
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        return False
    if fetched_at.utcoffset() != timezone.utc.utcoffset(fetched_at):
        return False

    age_seconds = Decimal(str((datetime.now(timezone.utc) - fetched_at).total_seconds()))
    return Decimal("0") <= age_seconds <= max_age


def _account_decimal(value: Any, asset: str, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise AccountValidationError(f"Missing {field} balance for {asset}")
    try:
        balance = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AccountValidationError(
            f"Invalid {field} balance for {asset}: {value!r}"
        ) from exc
    if not balance.is_finite() or balance < 0:
        raise AccountValidationError(
            f"Invalid {field} balance for {asset}: {value!r}"
        )
    return balance


def _account_asset_balance(balances: list[Any], asset: str) -> tuple[Decimal, Decimal]:
    matches = [
        balance for balance in balances
        if isinstance(balance, dict) and str(balance.get("asset", "")).upper() == asset
    ]
    if len(matches) != 1:
        raise AccountValidationError(f"Required account balance missing or ambiguous for {asset}")
    balance = matches[0]
    return (
        _account_decimal(balance.get("free"), asset, "free"),
        _account_decimal(balance.get("locked"), asset, "locked"),
    )


def fetch_account_snapshot(client, base_asset: str, quote_asset: str) -> AccountSnapshot:
    """Fetch required Spot balances through Binance's read-only account endpoint."""
    base = str(base_asset).upper()
    quote = str(quote_asset).upper()
    if not base or not quote or base == quote:
        raise AccountValidationError("Base and quote assets must be distinct, non-empty symbols")
    try:
        response = client.rest_api.get_account(omit_zero_balances=False)
        payload = _model_to_plain(response.data())
    except Exception as exc:
        raise AccountRequestError(
            f"Binance account information request failed for {base}/{quote}: {exc}"
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        raise AccountValidationError(
            f"Malformed Binance account response for {base}/{quote}: missing balances"
        )
    base_free, base_locked = _account_asset_balance(payload["balances"], base)
    quote_free, quote_locked = _account_asset_balance(payload["balances"], quote)
    return AccountSnapshot(
        base_asset=base,
        base_free=base_free,
        base_locked=base_locked,
        quote_asset=quote,
        quote_free=quote_free,
        quote_locked=quote_locked,
        fetched_at=datetime.now(timezone.utc),
    )


def calculate_spot_equity(snapshot: AccountSnapshot, current_ticker_price: Decimal) -> Decimal:
    """Mark the two-asset Spot account in its quote asset using a fresh ticker."""
    if not isinstance(snapshot, AccountSnapshot):
        raise AccountValidationError("A validated AccountSnapshot is required for equity")
    try:
        price = Decimal(str(current_ticker_price))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AccountValidationError("Ticker price is invalid for equity calculation") from exc
    if not price.is_finite() or price <= 0:
        raise AccountValidationError("Ticker price must be positive for equity calculation")
    return snapshot.quote_total + snapshot.base_total * price


def build_account_risk_state(
    snapshot: AccountSnapshot,
    current_ticker_price: Decimal,
    reference_equity: Decimal | None = None,
) -> AccountRiskState:
    """Build observation-only account risk state for the current process session.

    With no persisted trading ledger, a missing reference is deliberately set to
    this process's first valid observed equity; it is never treated as history.
    """
    current_equity = calculate_spot_equity(snapshot, current_ticker_price)
    raw_reference = current_equity if reference_equity is None else reference_equity
    try:
        reference = Decimal(str(raw_reference))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AccountValidationError("Reference equity is invalid") from exc
    if not reference.is_finite() or reference <= 0:
        raise AccountValidationError("Reference equity must be positive")
    if current_equity < 0:
        raise AccountValidationError("Current equity cannot be negative")

    base_inventory = snapshot.base_total
    inventory_quote = base_inventory * Decimal(str(current_ticker_price))
    inventory_pct = inventory_quote / current_equity if current_equity > 0 else Decimal("0")
    drawdown_pct = max(Decimal("0"), (reference - current_equity) / reference)
    return AccountRiskState(
        current_equity=current_equity,
        reference_equity=reference,
        drawdown_pct=drawdown_pct,
        base_inventory=base_inventory,
        inventory_pct=inventory_pct,
    )

def fetch_klines(client, symbol, interval="15m", limit=200, drop_incomplete=True):
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported interval: {interval}")
    response = client.rest_api.klines(
        symbol=symbol.upper(),
        interval=_enum_value(INTERVAL_MAP[interval]),
        limit=min(int(limit),1000),
    )
    rows = _model_to_plain(response.data())
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Binance returned no klines")

    columns = [
        "open_time","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_base","taker_quote","ignore",
    ]
    normalized = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < len(columns):
            raise RuntimeError("Unexpected Binance kline row shape")
        normalized.append(list(row[:len(columns)]))

    df = pd.DataFrame(normalized, columns=columns)
    for col in ["open","high","low","close","volume","quote_volume","taker_base","taker_quote"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open","high","low","close","volume"]).reset_index(drop=True)

    if drop_incomplete:
        now = datetime.now(timezone.utc)
        df = df[df["close_time"] <= pd.Timestamp(now)].reset_index(drop=True)
    if len(df) < 50:
        raise RuntimeError("Not enough closed klines after removing incomplete candle")
    return df

def fetch_symbol_info(client, symbol):
    response = client.rest_api.exchange_info(symbol=symbol.upper())
    payload = _model_to_plain(response.data())
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    if not symbols:
        raise RuntimeError(f"No exchangeInfo for {symbol}")
    info = symbols[0]
    if str(info.get("status","")).upper() != "TRADING":
        raise RuntimeError(f"Symbol {symbol} is not TRADING")
    permissions = {p for p in info.get("permissions",[]) if isinstance(p,str)}
    if permissions and "SPOT" not in permissions:
        raise RuntimeError(f"Symbol {symbol} does not expose SPOT permission")
    return info

def fetch_account_commission(client, symbol):
    try:
        response = client.rest_api.account_commission(symbol=symbol.upper())
        return _model_to_plain(response.data()), "ACCOUNT_COMMISSION"
    except Exception as exc:
        return None, f"FALLBACK:{type(exc).__name__}"
