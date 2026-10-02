"""Phase 6A: Binance Spot TESTNET read-only adapter.

Connects the existing paper/risk architecture to Binance Spot Testnet for
READ-ONLY market/account/order-state verification.

This adapter is NOT live trading.  Order submission is impossible by design.

Environment requirements:
    BINANCE_ENV=testnet
    BINANCE_BASE_URL=https://testnet.binance.vision
    BINANCE_API_KEY=<testnet-key>
    BINANCE_API_SECRET=<testnet-secret>
    DRY_RUN=true
    ALLOW_LIVE_EXECUTION=false
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from binance_sdk_spot.spot import Spot, ConfigurationRestAPI
except ImportError:
    Spot = ConfigurationRestAPI = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Approved testnet endpoints (absolute, no fallback)
# ---------------------------------------------------------------------------
_APPROVED_TESTNET_BASE = "https://testnet.binance.vision"
_APPROVED_TESTNET_REST = "https://testnet.binance.vision"
_REJECTED_PRODUCTION_BASES = frozenset({
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://sapi.binance.com",
    "https://fapi.binance.com",
    "https://testnet.binancefuture.com",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class BinanceTestnetError(RuntimeError):
    """Base exception for Binance testnet adapter errors."""


class BinanceTestnetConfigError(BinanceTestnetError):
    """Configuration validation failed.  Fail-closed — never falls back."""


class BinanceTestnetNetworkError(BinanceTestnetError):
    """Network / transport failure."""


class BinanceTestnetAuthenticationError(BinanceTestnetError):
    """Authentication failed (bad key/secret/signature)."""


class BinanceTestnetResponseError(BinanceTestnetError):
    """API returned an unexpected or unreadable response."""


class BinanceTestnetValidationError(BinanceTestnetError):
    """Response data failed validation checks."""


class BinanceTestnetEnvironmentError(BinanceTestnetError):
    """Environment is not the approved testnet."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
#: Defensive upper bound on open orders a single response may carry.  This is
#: a resource/response-size guard only — it must NOT replace the
#: PaperOrchestrator / order-engine capacity guard; both layers stay active.
DEFAULT_MAX_OPEN_ORDERS = 100
#: Defensive upper bound on the number of account asset rows in one response.
DEFAULT_MAX_ACCOUNT_ASSETS = 1000


@dataclass(frozen=True)
class BinanceTestnetConfig:
    """Immutable adapter configuration.  Validated at construction time.

    ``api_key`` and ``api_secret`` are excluded from the auto-generated
    ``repr()``/``str()`` via ``repr=False`` so that a config object printed
    in a log, traceback, or assert never exposes credentials (finding 6).
    """

    environment: str
    base_url: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    dry_run: bool
    allow_live_execution: bool
    timeout_ms: int = 5000
    retries: int = 3
    backoff_ms: int = 1000
    max_open_orders: int = DEFAULT_MAX_OPEN_ORDERS
    max_account_assets: int = DEFAULT_MAX_ACCOUNT_ASSETS

    def __post_init__(self) -> None:
        self._validate()

    # -- private validation (fail-closed) ------------------------------------
    def _validate(self) -> None:
        if self.environment != "testnet":
            raise BinanceTestnetConfigError(
                f"BINANCE_ENV must be 'testnet', got {self.environment!r}"
            )
        if not self.dry_run:
            raise BinanceTestnetConfigError(
                "DRY_RUN must be true"
            )
        if self.allow_live_execution:
            raise BinanceTestnetConfigError(
                "ALLOW_LIVE_EXECUTION must be false"
            )
        if self.base_url.rstrip("/") != _APPROVED_TESTNET_BASE:
            if self.base_url.rstrip("/") in _REJECTED_PRODUCTION_BASES:
                raise BinanceTestnetEnvironmentError(
                    f"Production URL rejected: {self.base_url!r}"
                )
            raise BinanceTestnetEnvironmentError(
                f"Base URL is not the approved testnet: {self.base_url!r}"
            )
        # Defensive response-size guards must be positive integers.
        if not isinstance(self.max_open_orders, int) or isinstance(self.max_open_orders, bool):
            raise BinanceTestnetConfigError("max_open_orders must be an integer")
        if self.max_open_orders < 1:
            raise BinanceTestnetConfigError("max_open_orders must be >= 1")
        if not isinstance(self.max_account_assets, int) or isinstance(self.max_account_assets, bool):
            raise BinanceTestnetConfigError("max_account_assets must be an integer")
        if self.max_account_assets < 1:
            raise BinanceTestnetConfigError("max_account_assets must be >= 1")

    # -- public helpers ------------------------------------------------------
    @property
    def rest_base_path(self) -> str:
        return _APPROVED_TESTNET_REST


def load_testnet_config_from_env() -> BinanceTestnetConfig:
    """Build configuration from environment variables.  Fail-closed."""
    def _env(name: str, *, required: bool = True) -> str:
        value = os.environ.get(name, "").strip()
        if required and not value:
            raise BinanceTestnetConfigError(
                f"Environment variable {name} is required"
            )
        return value

    def _strict_bool(name: str) -> bool:
        """Parse strict true/false, case-insensitive. Fail-closed on any other value."""
        val = _env(name).lower()
        if val not in ("true", "false"):
            raise BinanceTestnetConfigError(
                f"{name} must be 'true' or 'false' (got {val!r})"
            )
        return val == "true"

    def _positive_int(name: str, default: int, minimum: int) -> int:
        """Parse integer with validation. Raise BinanceTestnetConfigError on failure."""
        raw = _env(name, required=False)
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError as exc:
            raise BinanceTestnetConfigError(
                f"{name} must be an integer (got {raw!r})"
            ) from exc
        if val < minimum:
            raise BinanceTestnetConfigError(
                f"{name} must be >= {minimum} (got {val})"
            )
        return val

    timeout_ms = _positive_int("BINANCE_TIMEOUT_MS", 5000, 1)
    if timeout_ms > 30000:
        raise BinanceTestnetConfigError(
            f"BINANCE_TIMEOUT_MS must not exceed 30000 (got {timeout_ms})"
        )
    retries = _positive_int("BINANCE_RETRIES", 3, 0)
    backoff_ms = _positive_int("BINANCE_BACKOFF_MS", 1000, 0)
    # Defensive response-size guards (finding 4/G).  When the variables are
    # unset the defaults apply; when set they must be positive integers and are
    # passed through to the frozen config, which re-validates.
    max_open_orders = _positive_int(
        "BINANCE_MAX_OPEN_ORDERS", DEFAULT_MAX_OPEN_ORDERS, 1
    )
    max_account_assets = _positive_int(
        "BINANCE_MAX_ACCOUNT_ASSETS", DEFAULT_MAX_ACCOUNT_ASSETS, 1
    )

    return BinanceTestnetConfig(
        environment=_env("BINANCE_ENV"),
        base_url=_env("BINANCE_BASE_URL"),
        api_key=_env("BINANCE_API_KEY"),
        api_secret=_env("BINANCE_API_SECRET"),
        dry_run=_strict_bool("DRY_RUN"),
        allow_live_execution=_strict_bool("ALLOW_LIVE_EXECUTION"),
        timeout_ms=timeout_ms,
        retries=retries,
        backoff_ms=backoff_ms,
        max_open_orders=max_open_orders,
        max_account_assets=max_account_assets,
    )


# ---------------------------------------------------------------------------
# Frozen snapshots
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BinanceConnectivitySnapshot:
    environment: str
    base_url: str
    symbol: str
    server_time_ms: int
    local_time_ms: int
    skew_ms: int
    ticker_price: str
    account_available: bool
    open_order_count: int
    symbol_rules_valid: bool
    overall: str
    reason: str = ""


@dataclass(frozen=True)
class BinanceSymbolSnapshot:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    filters: dict[str, Any]
    raw_exchange_info: dict[str, Any]


@dataclass(frozen=True)
class BinanceTickerSnapshot:
    symbol: str
    price: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class BinanceAccountBalance:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class BinanceAccountSnapshot:
    balances: tuple[BinanceAccountBalance, ...]
    fetched_at: datetime


@dataclass(frozen=True)
class BinanceOpenOrderSnapshot:
    order_id: int
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    time_in_force: str
    is_working: bool


# ---------------------------------------------------------------------------
# Live-trading guard
# ---------------------------------------------------------------------------
def assert_testnet_read_only(config: BinanceTestnetConfig) -> None:
    """Prove environment == testnet, dry_run == true, allow_live == false."""
    config._validate()  # also validates base_url


# ---------------------------------------------------------------------------
# Helper: model → plain dict (matches market_data._model_to_plain)
# ---------------------------------------------------------------------------
def _model_to_plain(value: Any) -> Any:
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


def _validate_filter_consistency(filters: dict[str, Any], symbol: str) -> None:
    """Reject internally contradictory symbol filter configurations (finding 8/I).

    For each filter family that exposes paired bounds, verify the logical
    invariant that the lower bound does not exceed the upper bound.  A
    contradiction (e.g. ``minPrice > maxPrice``) is impossible exchange state
    and is rejected outright — it is never silently clamped.  Filters absent
    from the payload are skipped so optional filters do not block a symbol.
    """
    def _get(ft: str, key: str) -> Decimal | None:
        raw = filters.get(ft)
        if not isinstance(raw, dict):
            return None
        value = raw.get(key)
        if value is None:
            return None
        return _decimal(value, f"{ft}.{key}")

    pairs = (
        ("PRICE_FILTER", "minPrice", "maxPrice"),
        ("LOT_SIZE", "minQty", "maxQty"),
        ("MARKET_LOT_SIZE", "minQty", "maxQty"),
        ("MIN_NOTIONAL", "minNotional", "maxNotional"),
        ("NOTIONAL", "minNotional", "maxNotional"),
    )
    for ft, lo_key, hi_key in pairs:
        lo = _get(ft, lo_key)
        hi = _get(ft, hi_key)
        if lo is not None and hi is not None and lo > hi:
            raise BinanceTestnetValidationError(
                f"Contradictory {ft} for {symbol}: {lo_key}={lo} > {hi_key}={hi}"
            )
    for ft in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
        raw = filters.get(ft)
        if not isinstance(raw, dict):
            continue
        if ft == "PERCENT_PRICE":
            lo = _get(ft, "multiplierDown")
            hi = _get(ft, "multiplierUp")
            if lo is not None and hi is not None and lo > hi:
                raise BinanceTestnetValidationError(
                    f"Contradictory {ft} for {symbol}: "
                    f"multiplierDown={lo} > multiplierUp={hi}"
                )
        else:
            for side in ("BID", "ASK"):
                sub = raw.get(side)
                if not isinstance(sub, dict):
                    continue
                slo = _decimal(sub["multiplierDown"], f"{ft}.{side}.multiplierDown") \
                    if sub.get("multiplierDown") is not None else None
                shi = _decimal(sub["multiplierUp"], f"{ft}.{side}.multiplierUp") \
                    if sub.get("multiplierUp") is not None else None
                if slo is not None and shi is not None and slo > shi:
                    raise BinanceTestnetValidationError(
                        f"Contradictory {ft} {side} for {symbol}: "
                        f"multiplierDown={slo} > multiplierUp={shi}"
                    )


def _decimal(value: Any, field: str, *, finite: bool = True) -> Decimal:
    """Parse ``value`` to a Decimal, failing closed on malformed input.

    ``finite`` (default True) rejects NaN and +/-Infinity.  This is a hard
    requirement for any value that enters a financial calculation (finding 5):
    converting to ``Decimal`` alone does NOT reject the string forms
    ``'NaN'`` / ``'Infinity'`` / ``'-Infinity'``, so ``is_finite()`` is
    applied explicitly before the value is returned.
    """
    if value is None or isinstance(value, bool):
        raise BinanceTestnetValidationError(f"{field} must be numeric, got {value!r}")
    if isinstance(value, str):
        # Reject infinite/NaN string forms up front so the error is
        # deterministic and the offending literal is visible (never a secret).
        stripped = value.strip()
        if stripped.lower() in ("nan", "inf", "infinity", "+inf", "+infinity",
                                "-inf", "-infinity", "infinity"):
            raise BinanceTestnetValidationError(
                f"{field} is not a finite number: {value!r}"
            )
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BinanceTestnetValidationError(
            f"{field} is not a valid Decimal: {value!r}"
        ) from exc
    if finite and not d.is_finite():
        raise BinanceTestnetValidationError(f"{field} must be a finite number")
    return d


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class BinanceTestnetClient:
    """Read-only Binance Spot Testnet client.  Never exposes order methods."""

    def __init__(self, config: BinanceTestnetConfig) -> None:
        assert_testnet_read_only(config)
        if Spot is None or ConfigurationRestAPI is None:
            raise BinanceTestnetConfigError(
                "binance-sdk-spot is not installed"
            )
        self._config = config
        cfg = ConfigurationRestAPI(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_path=config.rest_base_path,
            timeout=config.timeout_ms,
            retries=config.retries,
            backoff=config.backoff_ms,
        )
        self._spot = Spot(config_rest_api=cfg)

    # -- read-only methods ---------------------------------------------------
    def ping(self) -> bool:
        """Test connectivity to the Binance Spot Testnet REST API.

        Returns ``True`` on success.  The ping endpoint returns no payload,
        so we only verify that the request completed without error.
        """
        try:
            self._spot.rest_api.ping()
        except Exception as exc:
            _raise_network_or_auth(exc)
        return True

    def server_time(self) -> tuple[int, int]:
        """Return (server_time_ms, local_time_ms)."""
        local_before = int(time.time() * 1000)
        try:
            resp = self._spot.rest_api.time()
        except Exception as exc:
            _raise_network_or_auth(exc)
        payload = _model_to_plain(resp.data())
        server_time = payload.get("serverTime")
        if not isinstance(server_time, int) or server_time <= 0:
            raise BinanceTestnetResponseError(
                f"Malformed serverTime: {server_time!r}"
            )
        return server_time, local_before

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        normalized = str(symbol).upper()
        try:
            resp = self._spot.rest_api.exchange_info(symbol=normalized)
        except Exception as exc:
            _raise_network_or_auth(exc)
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, dict):
            raise BinanceTestnetResponseError("exchangeInfo response is not an object")
        symbols = payload.get("symbols", [])
        if not isinstance(symbols, list) or not symbols:
            raise BinanceTestnetValidationError(
                f"No symbols in exchangeInfo for {normalized}"
            )
        info = symbols[0]
        if str(info.get("symbol", "")).upper() != normalized:
            raise BinanceTestnetValidationError(
                f"exchangeInfo symbol mismatch: expected {normalized}, "
                f"got {info.get('symbol')!r}"
            )
        return info

    def ticker_price(self, symbol: str) -> BinanceTickerSnapshot:
        normalized = str(symbol).upper()
        try:
            resp = self._spot.rest_api.ticker_price(symbol=normalized)
        except Exception as exc:
            _raise_network_or_auth(exc)
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, dict):
            raise BinanceTestnetResponseError("ticker response is not an object")
        returned = payload.get("symbol")
        if returned is not None and str(returned).upper() != normalized:
            raise BinanceTestnetValidationError(
                f"Ticker symbol mismatch: expected {normalized}, got {returned!r}"
            )
        raw_price = payload.get("price")
        price = _decimal(raw_price, "price")
        if not price.is_finite() or price <= 0:
            raise BinanceTestnetValidationError(
                f"Invalid ticker price: {raw_price!r}"
            )
        return BinanceTickerSnapshot(
            symbol=normalized,
            price=price,
            fetched_at=datetime.now(timezone.utc),
        )

    def account(self) -> BinanceAccountSnapshot:
        try:
            resp = self._spot.rest_api.get_account(omit_zero_balances=False)
        except Exception as exc:
            _raise_network_or_auth(exc)
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, dict):
            raise BinanceTestnetResponseError("account response is not an object")
        raw_balances = payload.get("balances")
        if not isinstance(raw_balances, list):
            raise BinanceTestnetValidationError("account balances is not a list")
        if len(raw_balances) > self._config.max_account_assets:
            raise BinanceTestnetValidationError(
                f"account response lists {len(raw_balances)} assets, exceeding "
                f"the configured maximum of {self._config.max_account_assets}"
            )
        balances: list[BinanceAccountBalance] = []
        seen_assets: set[str] = set()
        for item in raw_balances:
            if not isinstance(item, dict):
                raise BinanceTestnetValidationError(
                    "Account balance entry is not an object"
                )
            asset = str(item.get("asset", "")).upper()
            if not asset:
                raise BinanceTestnetValidationError(
                    "Account balance entry has empty asset"
                )
            if asset in seen_assets:
                # Finding 6: duplicate asset rows make balances ambiguous.
                # Do NOT sum, first, last, or deduplicate — fail closed.
                raise BinanceTestnetValidationError(
                    f"Duplicate asset in account response: {asset}"
                )
            seen_assets.add(asset)
            free = _decimal(item.get("free"), f"{asset}.free")
            locked = _decimal(item.get("locked"), f"{asset}.locked")
            if not free.is_finite() or free < 0:
                raise BinanceTestnetValidationError(
                    f"Negative free balance for {asset}: {free}"
                )
            if not locked.is_finite() or locked < 0:
                raise BinanceTestnetValidationError(
                    f"Negative locked balance for {asset}: {locked}"
                )
            balances.append(BinanceAccountBalance(asset=asset, free=free, locked=locked))
        return BinanceAccountSnapshot(
            balances=tuple(balances),
            fetched_at=datetime.now(timezone.utc),
        )

    def open_orders(self, symbol: str) -> tuple[BinanceOpenOrderSnapshot, ...]:
        normalized = str(symbol).upper()
        try:
            resp = self._spot.rest_api.get_open_orders(symbol=normalized)
        except Exception as exc:
            _raise_network_or_auth(exc)
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, list):
            raise BinanceTestnetResponseError(
                "open_orders response is not a list"
            )
        if not payload:
            return ()
        # Finding 4/G: reject an open-order response that exceeds the
        # configured maximum instead of silently truncating or processing an
        # unexpectedly huge payload.  This guard is in ADDITION to the
        # PaperOrchestrator/order-engine capacity guard; both remain active.
        if len(payload) > self._config.max_open_orders:
            raise BinanceTestnetValidationError(
                f"open_orders response lists {len(payload)} orders, exceeding "
                f"the configured maximum of {self._config.max_open_orders}"
            )
        orders: list[BinanceOpenOrderSnapshot] = []
        for raw in payload:
            orders.append(_parse_open_order(raw, normalized))
        ids = [o.order_id for o in orders]
        cids = [o.client_order_id for o in orders]
        if len(set(ids)) != len(ids):
            raise BinanceTestnetValidationError(
                f"Duplicate orderId in open_orders for {normalized}"
            )
        if len(set(cids)) != len(cids):
            raise BinanceTestnetValidationError(
                f"Duplicate clientOrderId in open_orders for {normalized}"
            )
        return tuple(orders)

    def symbol_snapshot(self, symbol: str) -> BinanceSymbolSnapshot:
        """Aggregate snapshot: exchange info + all filters."""
        info = self.exchange_info(symbol)
        filters = info.get("filters", [])
        if not isinstance(filters, list):
            raise BinanceTestnetValidationError(
                f"exchangeInfo filters is not a list for {symbol}"
            )
        filter_map: dict[str, Any] = {}
        for f in filters:
            if isinstance(f, dict):
                ft = f.get("filterType")
                if isinstance(ft, str):
                    filter_map[ft] = f
        # Validate critical filters exist
        for critical in ("PRICE_FILTER", "LOT_SIZE"):
            if critical not in filter_map:
                raise BinanceTestnetValidationError(
                    f"Missing critical filter {critical} for {symbol}"
                )
        # Reject contradictory filter bounds (finding 8/I) before exposing
        # the snapshot to downstream allocation/planning.
        _validate_filter_consistency(filter_map, symbol)
        return BinanceSymbolSnapshot(
            symbol=str(info.get("symbol", "")).upper(),
            base_asset=str(info.get("baseAsset", "")),
            quote_asset=str(info.get("quoteAsset", "")),
            status=str(info.get("status", "")),
            filters=filter_map,
            raw_exchange_info=info,
        )

    def connectivity_check(self, symbol: str) -> BinanceConnectivitySnapshot:
        """Sequential read-only diagnostic.  Fail-closed on any error."""
        normalized = str(symbol).upper()
        # 1. config already validated at __init__
        # 2. ping
        try:
            self.ping()
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=0,
                local_time_ms=0,
                skew_ms=0,
                ticker_price="",
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=False,
                overall="FAIL",
                reason=f"PING_FAILED:{type(exc).__name__}",
            )
        # 3. server time
        try:
            server_ms, local_ms = self.server_time()
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=0,
                local_time_ms=0,
                skew_ms=0,
                ticker_price="",
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=False,
                overall="FAIL",
                reason=f"SERVER_TIME_FAILED:{type(exc).__name__}",
            )
        skew = server_ms - local_ms
        # 4. exchangeInfo
        try:
            snap = self.symbol_snapshot(normalized)
            symbol_rules_valid = snap.status.upper() == "TRADING"
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price="",
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=False,
                overall="FAIL",
                reason=f"EXCHANGE_INFO_FAILED:{type(exc).__name__}",
            )
        # 5. ticker
        try:
            ticker = self.ticker_price(normalized)
            ticker_str = str(ticker.price)
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price="",
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=symbol_rules_valid,
                overall="FAIL",
                reason=f"TICKER_FAILED:{type(exc).__name__}",
            )
        # 6. account (may require auth)
        try:
            acct = self.account()
            account_available = True
        except BinanceTestnetAuthenticationError as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price=ticker_str,
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=symbol_rules_valid,
                overall="FAIL",
                reason=f"ACCOUNT_AUTH_FAILED:{type(exc).__name__}",
            )
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price=ticker_str,
                account_available=False,
                open_order_count=0,
                symbol_rules_valid=symbol_rules_valid,
                overall="FAIL",
                reason=f"ACCOUNT_FAILED:{type(exc).__name__}",
            )
        # 7. open orders (may require auth)
        try:
            oo = self.open_orders(normalized)
            open_count = len(oo)
        except BinanceTestnetAuthenticationError as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price=ticker_str,
                account_available=account_available,
                open_order_count=0,
                symbol_rules_valid=symbol_rules_valid,
                overall="FAIL",
                reason=f"OPEN_ORDERS_AUTH_FAILED:{type(exc).__name__}",
            )
        except Exception as exc:
            return BinanceConnectivitySnapshot(
                environment=self._config.environment,
                base_url=self._config.base_url,
                symbol=normalized,
                server_time_ms=server_ms,
                local_time_ms=local_ms,
                skew_ms=skew,
                ticker_price=ticker_str,
                account_available=account_available,
                open_order_count=0,
                symbol_rules_valid=symbol_rules_valid,
                overall="FAIL",
                reason=f"OPEN_ORDERS_FAILED:{type(exc).__name__}",
            )
        return BinanceConnectivitySnapshot(
            environment=self._config.environment,
            base_url=self._config.base_url,
            symbol=normalized,
            server_time_ms=server_ms,
            local_time_ms=local_ms,
            skew_ms=skew,
            ticker_price=ticker_str,
            account_available=account_available,
            open_order_count=open_count,
            symbol_rules_valid=symbol_rules_valid,
            overall="PASS",
        )


# ---------------------------------------------------------------------------
# Open-order parser
# ---------------------------------------------------------------------------
_OPEN_ORDER_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_LIMIT_STYLE_TYPES = frozenset({
    "LIMIT", "LIMIT_MAKER", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT",
})


def _parse_open_order(raw: Any, symbol: str) -> BinanceOpenOrderSnapshot:
    if not isinstance(raw, dict):
        raise BinanceTestnetValidationError("Open order entry is not an object")
    raw_order_id = raw.get("orderId")
    if raw_order_id is None or isinstance(raw_order_id, bool):
        raise BinanceTestnetValidationError(
            f"Missing orderId for open order on {symbol}"
        )
    if isinstance(raw_order_id, int):
        order_id = raw_order_id
    elif isinstance(raw_order_id, str) and raw_order_id.isdigit():
        order_id = int(raw_order_id)
    else:
        raise BinanceTestnetValidationError(
            f"Invalid orderId for open order on {symbol}"
        )
    if order_id <= 0:
        raise BinanceTestnetValidationError(
            f"Invalid orderId (<=0) for open order on {symbol}"
        )
    client_order_id = raw.get("clientOrderId")
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        raise BinanceTestnetValidationError(
            f"Missing clientOrderId for open order on {symbol}"
        )
    returned_symbol = raw.get("symbol")
    if not isinstance(returned_symbol, str) or returned_symbol.upper() != symbol:
        raise BinanceTestnetValidationError(
            f"Open order symbol mismatch: expected {symbol}, received {returned_symbol!r}"
        )
    side = raw.get("side")
    if not isinstance(side, str) or side.upper() not in {"BUY", "SELL"}:
        raise BinanceTestnetValidationError(
            f"Invalid side for open order on {symbol}: {side!r}"
        )
    order_type = raw.get("type")
    if not isinstance(order_type, str) or not order_type.strip():
        raise BinanceTestnetValidationError(
            f"Missing type for open order on {symbol}"
        )
    status = raw.get("status")
    if not isinstance(status, str) or status.upper() not in _OPEN_ORDER_STATUSES:
        raise BinanceTestnetValidationError(
            f"Unexpected open-order status for {symbol}: {status!r}"
        )
    time_in_force = raw.get("timeInForce")
    if not isinstance(time_in_force, str) or not time_in_force.strip():
        raise BinanceTestnetValidationError(
            f"Missing timeInForce for open order on {symbol}"
        )
    is_working = raw.get("isWorking")
    if not isinstance(is_working, bool):
        raise BinanceTestnetValidationError(
            f"Invalid isWorking for open order on {symbol}"
        )
    price = _decimal(raw.get("price"), "price")
    orig_qty = _decimal(raw.get("origQty"), "origQty")
    executed_qty = _decimal(raw.get("executedQty"), "executedQty")
    if not price.is_finite() or price < 0:
        raise BinanceTestnetValidationError(
            f"Negative or non-finite price for open order on {symbol}: {price}"
        )
    if order_type.upper() in _LIMIT_STYLE_TYPES and price <= 0:
        raise BinanceTestnetValidationError(
            f"Limit-style open order has invalid price on {symbol}"
        )
    if not orig_qty.is_finite() or orig_qty <= 0:
        raise BinanceTestnetValidationError(
            f"Open order has invalid origQty on {symbol}"
        )
    if not executed_qty.is_finite() or executed_qty < 0:
        raise BinanceTestnetValidationError(
            f"Negative executedQty for open order on {symbol}: {executed_qty}"
        )
    if executed_qty > orig_qty:
        raise BinanceTestnetValidationError(
            f"Open order executedQty exceeds origQty on {symbol}"
        )
    return BinanceOpenOrderSnapshot(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side.upper(),
        order_type=order_type.upper(),
        status=status.upper(),
        price=price,
        orig_qty=orig_qty,
        executed_qty=executed_qty,
        time_in_force=time_in_force.upper(),
        is_working=is_working,
    )


# ---------------------------------------------------------------------------
# Error mapper
# ---------------------------------------------------------------------------
AUTH_INDICATORS = frozenset(("unauthorized", "invalid apikey", "signature",
                              "401", "403", "forbidden"))


def _redact_credentials(msg: str) -> str:
    """Redact potential API key/secret fragments from SDK exception messages.

    Replaces any occurrence of common credential-bearing substrings with a
    placeholder, while preserving the rest of the message so that auth-vs-network
    classification remains accurate.
    """
    lowered = msg.lower()
    for indicator in AUTH_INDICATORS:
        if indicator in lowered:
            # Auth-related messages almost always carry key/secret context;
            # redact the whole message to be safe.
            return "[credentials-redacted]"
    for pattern in ("api_key=", "apikey=", "api-secret=", "apikey=",
                    "secret=", "key="):
        if pattern in lowered:
            return "[credentials-redacted]"
    return msg


def _raise_network_or_auth(exc: Exception) -> None:
    raw_msg = str(exc)
    lowered = raw_msg.lower()
    if any(k in lowered for k in AUTH_INDICATORS):
        raise BinanceTestnetAuthenticationError(_redact_credentials(raw_msg)) from exc
    raise BinanceTestnetNetworkError(_redact_credentials(raw_msg)) from exc
