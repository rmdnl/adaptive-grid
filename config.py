"""Configuration: `.env` is the single source of truth.

There is no YAML configuration and there are no hardcoded strategy
fallbacks: a missing or invalid required key fails startup with a clear
error listing every problem found. All other modules receive validated
values from the immutable Config object and never read the environment
themselves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from dotenv import dotenv_values


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


ALLOWED_TIMEFRAMES = (
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h", "1d",
)

# Execution modes: paper = internal simulation (no orders, capital derived
# from the testnet USDT balance); testnet = real Binance Spot TESTNET orders;
# live = gated production execution (locked by default).
ALLOWED_EXECUTION_MODES = ("paper", "testnet", "live")

# Grid type per symbol. Configuration-level mapping only; unknown symbols
# default to arithmetic.
GRID_MODES = {
    "BTC/USDT": "arithmetic",
    "ETH/USDT": "arithmetic",
    "BNB/USDT": "arithmetic",
    "SOL/USDT": "geometric",
}

# Capital-protection floors (hard limits; configuration cannot go below).
MIN_GROSS_FLOOR = 0.005            # gross profit per grid >= 0.50%
MIN_NET_FLOOR = 0.002              # net profit per grid >= 0.20%
MAX_DRAWDOWN_CAP_PERCENT = 2.0     # global drawdown kill switch <= 2%
STOP_BELOW_LOWER_CAP_PERCENT = 2.0

_PAIR_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+$")


@dataclass(frozen=True)
class Config:
    binance_env: str
    dry_run: bool
    allow_live_execution: bool

    pair_list: Tuple[str, ...]
    indicator_timeframe: str

    adx_period: int
    rsi_period: int
    bb_period: int
    bb_std: float
    vo_fast: int
    vo_slow: int
    zscore_period: int
    atr_period: int

    entry_adx_max: float
    entry_rsi_max: float
    entry_volume_osc_min: float
    entry_bb_percent_b_max: float

    exit_rsi_min: float
    exit_adx_min: float
    exit_bb_percent_b_min: float
    exit_zscore_abs_max: float

    grid_step_atr_multiplier: float
    grid_gross_min: float
    min_net_profit_per_grid: float

    maker_fee: float
    taker_fee: float
    slippage_estimate: float

    max_drawdown_percent: float
    stop_if_below_lower_percent: float

    cooldown_hours: float

    # Execution mode: paper (internal simulation, no orders), testnet (real
    # Binance Spot TESTNET orders) or live (gated production). See
    # ALLOWED_EXECUTION_MODES and the fail-closed consistency rules below.
    execution_mode: str

    # Optional manual session capital override. 0 (the default) means:
    # derive the session capital from the Binance testnet USDT balance at
    # session creation. The session capital anchors the PnL-based equity
    # model (equity = capital + realized - fees + unrealized), so the 2%
    # drawdown limit is a percentage of a real capital base.
    start_equity: float

    # Market data freshness (Phase 7 hardening)
    max_market_data_age_seconds: float

    # Adaptive Spot Grid (Phase 1: automatic grid parameters)
    # When true (default), LOWER_PRICE, UPPER_PRICE, TOTAL_GRIDS, TOTAL_QUOTE_BUDGET
    # are computed automatically and need not be configured.
    adaptive_grid: bool
    # Minimum and maximum candidate grid counts for automatic selection.
    min_grids: int
    max_grids: int
    # Reserve % of available USDT that must not be allocated.
    quote_reserve_percent: float
    # Maximum % of available USDT (after reserve) that can be allocated across all symbols.
    max_quote_allocation_percent: float

    # Grid range and budget (per-symbol, JSON maps symbol -> value).
    # OPTIONAL when adaptive_grid=true. Required when adaptive_grid=false.
    lower_price: Dict[str, float]
    upper_price: Dict[str, float]
    total_grids: int
    total_quote_budget: Dict[str, float]

    testnet_api_key: str
    testnet_api_secret: str
    live_api_key: str
    live_api_secret: str

    env_file: str

    @property
    def allow_live(self) -> bool:
        """Live endpoints/credentials are used only when ALL gates are
        explicitly satisfied: EXECUTION_MODE=live AND BINANCE_ENV=live AND
        DRY_RUN=false AND ALLOW_LIVE_EXECUTION=true."""
        return (
            self.execution_mode == "live"
            and self.binance_env == "live"
            and (not self.dry_run)
            and self.allow_live_execution
        )

    @property
    def api_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Credentials for the active environment. Live credentials are
        never returned unless `allow_live` is true."""
        if self.allow_live:
            return self.live_api_key, self.live_api_secret
        return self.testnet_api_key, self.testnet_api_secret

    @property
    def max_drawdown(self) -> float:
        return self.max_drawdown_percent / 100.0

    @property
    def stop_if_below_lower(self) -> float:
        return self.stop_if_below_lower_percent / 100.0

    def grid_mode(self, symbol: str) -> str:
        return GRID_MODES.get(symbol, "arithmetic")


def _missing(env: dict, key: str) -> bool:
    raw = env.get(key)
    return raw is None or str(raw).strip() == ""


def _get_bool(env: dict, key: str, default: Optional[bool], errors: list) -> Optional[bool]:
    if _missing(env, key):
        if default is None:
            errors.append(f"missing required key: {key}")
            return None
        return default
    val = str(env[key]).strip().lower()
    if val == "true":
        return True
    if val == "false":
        return False
    errors.append(f"invalid value for {key}: {env[key]!r} (expected true/false)")
    return None


def _get_str(env: dict, key: str, default: Optional[str], errors: list) -> Optional[str]:
    if _missing(env, key):
        if default is None:
            errors.append(f"missing required key: {key}")
            return None
        return default
    return str(env[key]).strip()


def _get_int(
    env: dict,
    key: str,
    minimum: Optional[int],
    errors: list,
    default: Optional[int] = None,
) -> Optional[int]:
    if _missing(env, key):
        if default is None:
            errors.append(f"missing required key: {key}")
            return None
        return default
    try:
        val = int(str(env[key]).strip())
    except ValueError:
        errors.append(f"invalid integer for {key}: {env[key]!r}")
        return None
    if minimum is not None and val < minimum:
        errors.append(f"{key} must be >= {minimum}, got {val}")
        return None
    return val


def _get_float(
    env: dict,
    key: str,
    minimum: Optional[float],
    maximum: Optional[float],
    errors: list,
    default: Optional[float] = None,
) -> Optional[float]:
    if _missing(env, key):
        if default is None:
            errors.append(f"missing required key: {key}")
            return None
        return default
    try:
        val = float(str(env[key]).strip())
    except ValueError:
        errors.append(f"invalid number for {key}: {env[key]!r}")
        return None
    if minimum is not None and val < minimum:
        errors.append(f"{key} must be >= {minimum}, got {val}")
        return None
    if maximum is not None and val > maximum:
        errors.append(f"{key} must be <= {maximum}, got {val}")
        return None
    return val


def _parse_pair_list(raw: str, errors: list) -> Tuple[str, ...]:
    pairs = []
    for part in raw.split(","):
        pair = part.strip().upper()
        if not pair:
            continue
        if not _PAIR_RE.match(pair):
            errors.append(f"invalid pair in PAIR_LIST: {part!r} (expected BASE/QUOTE)")
            continue
        if pair not in pairs:
            pairs.append(pair)
    if not pairs:
        errors.append("PAIR_LIST must contain at least one pair")
    return tuple(pairs)


def _parse_json_map(raw: str, key: str, errors: list) -> Dict[str, float]:
    """Parse a JSON object mapping symbol -> float value."""
    import json
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"{key} must be valid JSON object: {exc}")
        return {}
    if not isinstance(parsed, dict):
        errors.append(f"{key} must be a JSON object, got {type(parsed).__name__}")
        return {}
    out = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not _PAIR_RE.match(k):
            errors.append(f"{key} key {k!r} is not a valid symbol (expected BASE/QUOTE)")
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            errors.append(f"{key} value for {k!r} must be a number, got {v!r}")
            continue
        out[k] = fv
    return out


def load_config(env_file: str = ".env") -> Config:
    """Load, parse and validate configuration from a single `.env` file."""
    if not os.path.isfile(env_file):
        raise ConfigError(f"configuration file not found: {env_file}")

    env = {k: v for k, v in dotenv_values(env_file).items()}
    errors: list = []

    binance_env = _get_str(env, "BINANCE_ENV", default="testnet", errors=errors)
    if binance_env is not None and binance_env not in ("testnet", "live"):
        errors.append(f"BINANCE_ENV must be 'testnet' or 'live', got {binance_env!r}")
        binance_env = None

    dry_run = _get_bool(env, "DRY_RUN", default=True, errors=errors)
    allow_live_execution = _get_bool(env, "ALLOW_LIVE_EXECUTION", default=False, errors=errors)

    pair_raw = _get_str(env, "PAIR_LIST", default=None, errors=errors)
    pair_list: Tuple[str, ...] = ()
    if pair_raw is not None:
        pair_list = _parse_pair_list(pair_raw, errors)

    indicator_timeframe = _get_str(env, "INDICATOR_TIMEFRAME", default=None, errors=errors)
    if indicator_timeframe is not None and indicator_timeframe not in ALLOWED_TIMEFRAMES:
        errors.append(
            f"INDICATOR_TIMEFRAME must be one of {ALLOWED_TIMEFRAMES}, got {indicator_timeframe!r}"
        )
        indicator_timeframe = None

    adx_period = _get_int(env, "ADX_PERIOD", minimum=1, errors=errors)
    rsi_period = _get_int(env, "RSI_PERIOD", minimum=1, errors=errors)
    bb_period = _get_int(env, "BB_PERIOD", minimum=2, errors=errors)
    bb_std = _get_float(env, "BB_STD", minimum=0.000001, maximum=None, errors=errors)
    vo_fast = _get_int(env, "VO_FAST", minimum=1, errors=errors)
    vo_slow = _get_int(env, "VO_SLOW", minimum=2, errors=errors)
    if vo_fast is not None and vo_slow is not None and vo_fast >= vo_slow:
        errors.append("VO_FAST must be < VO_SLOW")
    zscore_period = _get_int(env, "ZSCORE_PERIOD", minimum=2, errors=errors)
    atr_period = _get_int(env, "ATR_PERIOD", minimum=1, errors=errors)

    entry_adx_max = _get_float(env, "ENTRY_ADX_MAX", minimum=0.000001, maximum=None, errors=errors)
    entry_rsi_max = _get_float(env, "ENTRY_RSI_MAX", minimum=0.0, maximum=100.0, errors=errors)
    entry_volume_osc_min = _get_float(env, "ENTRY_VOLUME_OSC_MIN", minimum=None, maximum=None, errors=errors)
    entry_bb_percent_b_max = _get_float(env, "ENTRY_BB_PERCENT_B_MAX", minimum=None, maximum=None, errors=errors)

    exit_rsi_min = _get_float(env, "EXIT_RSI_MIN", minimum=0.000001, maximum=100.0, errors=errors)
    exit_adx_min = _get_float(env, "EXIT_ADX_MIN", minimum=0.000001, maximum=None, errors=errors)
    exit_bb_percent_b_min = _get_float(env, "EXIT_BB_PERCENT_B_MIN", minimum=None, maximum=None, errors=errors)
    exit_zscore_abs_max = _get_float(env, "EXIT_ZSCORE_ABS_MAX", minimum=0.000001, maximum=None, errors=errors)

    grid_step_atr_multiplier = _get_float(env, "GRID_STEP_ATR_MULTIPLIER", minimum=0.000001, maximum=None, errors=errors)
    grid_gross_min = _get_float(env, "GRID_GROSS_MIN", minimum=MIN_GROSS_FLOOR, maximum=None, errors=errors)
    min_net_profit_per_grid = _get_float(env, "MIN_NET_PROFIT_PER_GRID", minimum=MIN_NET_FLOOR, maximum=None, errors=errors)

    maker_fee = _get_float(env, "MAKER_FEE", minimum=0.0, maximum=None, errors=errors)
    taker_fee = _get_float(env, "TAKER_FEE", minimum=0.0, maximum=None, errors=errors)
    slippage_estimate = _get_float(env, "SLIPPAGE_ESTIMATE", minimum=0.0, maximum=None, errors=errors)

    max_drawdown_percent = _get_float(
        env, "MAX_DRAWDOWN_PERCENT", minimum=0.000001, maximum=MAX_DRAWDOWN_CAP_PERCENT, errors=errors
    )
    stop_if_below_lower_percent = _get_float(
        env, "STOP_IF_BELOW_LOWER_PERCENT", minimum=0.000001, maximum=STOP_BELOW_LOWER_CAP_PERCENT, errors=errors
    )

    cooldown_hours = _get_float(env, "COOLDOWN_HOURS", minimum=0.0, maximum=None, errors=errors)

    # Phase 7: Market data freshness (conservative default for 4h strategy + 15m risk)
    max_market_data_age_seconds = _get_float(
        env, "MAX_MARKET_DATA_AGE_SECONDS", minimum=1.0, maximum=None, errors=errors, default=21600.0
    )

    # 0 (or absent) = derive the session capital from the testnet USDT balance.
    start_equity = _get_float(env, "START_EQUITY", default=0.0, minimum=0.0, maximum=None, errors=errors)

    # Adaptive Spot Grid configuration
    # When adaptive_grid=true (default), LOWER_PRICE, UPPER_PRICE, TOTAL_GRIDS,
    # TOTAL_QUOTE_BUDGET are computed automatically and need not be configured.
    adaptive_grid = _get_bool(env, "ADAPTIVE_GRID", default=True, errors=errors)
    min_grids = _get_int(env, "MIN_GRIDS", minimum=1, errors=errors, default=3)
    max_grids = _get_int(env, "MAX_GRIDS", minimum=1, errors=errors, default=12)
    if min_grids is not None and max_grids is not None and min_grids > max_grids:
        errors.append("MIN_GRIDS must be <= MAX_GRIDS")
    quote_reserve_percent = _get_float(env, "QUOTE_RESERVE_PERCENT", minimum=0.0, maximum=100.0, errors=errors, default=20.0)
    max_quote_allocation_percent = _get_float(env, "MAX_QUOTE_ALLOCATION_PERCENT", minimum=0.0, maximum=100.0, errors=errors, default=80.0)

    # Grid range and budget configuration (OPTIONAL when adaptive_grid=true)
    # Only parse and validate these when adaptive_grid=false
    lower_price: Dict[str, float] = {}
    upper_price: Dict[str, float] = {}
    total_grids: Optional[int] = None
    total_quote_budget: Dict[str, float] = {}

    if adaptive_grid is False:
        lower_price_raw = _get_str(env, "LOWER_PRICE", default=None, errors=errors)
        if lower_price_raw is not None:
            lower_price = _parse_json_map(lower_price_raw, "LOWER_PRICE", errors)

        upper_price_raw = _get_str(env, "UPPER_PRICE", default=None, errors=errors)
        if upper_price_raw is not None:
            upper_price = _parse_json_map(upper_price_raw, "UPPER_PRICE", errors)

        total_grids = _get_int(env, "TOTAL_GRIDS", minimum=1, errors=errors)
        if total_grids is None:
            errors.append("TOTAL_GRIDS must be explicitly configured when ADAPTIVE_GRID=false")

        total_quote_budget_raw = _get_str(env, "TOTAL_QUOTE_BUDGET", default=None, errors=errors)
        if total_quote_budget_raw is not None:
            total_quote_budget = _parse_json_map(total_quote_budget_raw, "TOTAL_QUOTE_BUDGET", errors)

    # Validate per-symbol grid range and budget configuration
    # Only required when adaptive_grid=false
    if adaptive_grid is False:
        for symbol in pair_list:
            if symbol not in lower_price:
                errors.append(f"LOWER_PRICE must be configured for all symbols in PAIR_LIST: missing {symbol}")
            elif lower_price[symbol] <= 0:
                errors.append(f"LOWER_PRICE for {symbol} must be > 0, got {lower_price[symbol]}")
            if symbol not in upper_price:
                errors.append(f"UPPER_PRICE must be configured for all symbols in PAIR_LIST: missing {symbol}")
            elif upper_price[symbol] <= 0:
                errors.append(f"UPPER_PRICE for {symbol} must be > 0, got {upper_price[symbol]}")
            if symbol in lower_price and symbol in upper_price:
                if lower_price[symbol] >= upper_price[symbol]:
                    errors.append(f"LOWER_PRICE for {symbol} ({lower_price[symbol]}) must be < UPPER_PRICE ({upper_price[symbol]})")
            if symbol not in total_quote_budget:
                errors.append(f"TOTAL_QUOTE_BUDGET must be configured for all symbols in PAIR_LIST: missing {symbol}")
            elif total_quote_budget[symbol] <= 0:
                errors.append(f"TOTAL_QUOTE_BUDGET for {symbol} must be > 0, got {total_quote_budget[symbol]}")

    # Phase 9: Additional validation for invalid combinations
    # 1. Validate that max_market_data_age_seconds is not too small for the indicator timeframe
    #    (minimum the timeframe interval to get at least one closed candle)
    timeframe_to_seconds = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
    }
    if indicator_timeframe in timeframe_to_seconds:
        min_age = timeframe_to_seconds[indicator_timeframe]
        if max_market_data_age_seconds < min_age:
            errors.append(
                f"MAX_MARKET_DATA_AGE_SECONDS ({max_market_data_age_seconds}) must be >= "
                f"the indicator timeframe ({min_age}s for {indicator_timeframe})"
            )

    # 2. Validate that grid range can accommodate TOTAL_GRIDS levels
    #    Only when static grid parameters are provided (adaptive_grid=false or manually configured)
    if total_grids is not None:
        for symbol in pair_list:
            if symbol in lower_price and symbol in upper_price:
                price_range = upper_price[symbol] - lower_price[symbol]
                if price_range <= 0:
                    continue
                # Minimum step needed for total_grids levels
                min_step = price_range / total_grids
                # The grid step is derived from ATR, but we can at least validate
                # that the range is not absurdly small
                if price_range / upper_price[symbol] < 0.001:  # less than 0.1% range
                    errors.append(
                        f"Grid range for {symbol} too small: "
                        f"UPPER_PRICE ({upper_price[symbol]}) - LOWER_PRICE ({lower_price[symbol]}) "
                        f"= {price_range} ({price_range/upper_price[symbol]*100:.3f}% of price)"
                    )

    # 3. Validate TOTAL_QUOTE_BUDGET is sufficient for minimum notional
    #    (at least min_notional * TOTAL_GRIDS per symbol).
    #    We cannot validate this here without exchange filters; actual
    #    enforcement happens in grid building with real min_notional values.

    # 4. Validate grid step ATR multiplier is reasonable
    if grid_step_atr_multiplier is not None:
        if grid_step_atr_multiplier < 0.1:
            errors.append("GRID_STEP_ATR_MULTIPLIER must be >= 0.1")
        if grid_step_atr_multiplier > 10.0:
            errors.append("GRID_STEP_ATR_MULTIPLIER should not exceed 10.0 (excessive step)")

    # 5. Validate that the budget is not excessive relative to the grid range
    #    Only when static grid parameters are provided
    if total_grids is not None:
        for symbol in pair_list:
            if symbol in total_quote_budget and symbol in lower_price and symbol in upper_price:
                budget = total_quote_budget[symbol]
                price_range = upper_price[symbol] - lower_price[symbol]
                if price_range > 0:
                    budget_pct_of_range = budget / (upper_price[symbol] * total_grids)
                    if budget_pct_of_range > 10.0:  # budget > 10x notional per grid
                        errors.append(
                            f"TOTAL_QUOTE_BUDGET for {symbol} ({budget}) appears excessive "
                            f"relative to grid range ({price_range}) and {total_grids} grids"
                        )

    execution_mode = _get_str(env, "EXECUTION_MODE", default="paper", errors=errors)
    if execution_mode is not None and execution_mode not in ALLOWED_EXECUTION_MODES:
        errors.append(
            f"EXECUTION_MODE must be one of {ALLOWED_EXECUTION_MODES}, got {execution_mode!r}"
        )
        execution_mode = None

    testnet_api_key = str(env.get("BINANCE_TESTNET_API_KEY") or "").strip()
    testnet_api_secret = str(env.get("BINANCE_TESTNET_API_SECRET") or "").strip()
    live_api_key = str(env.get("BINANCE_API_KEY") or "").strip()
    live_api_secret = str(env.get("BINANCE_API_SECRET") or "").strip()

    if errors:
        raise ConfigError("invalid configuration:\n- " + "\n- ".join(errors))

    # ----- execution-mode consistency (fail closed) -----
    # Allowed combinations:
    #   BINANCE_ENV=testnet + EXECUTION_MODE=paper
    #   BINANCE_ENV=testnet + EXECUTION_MODE=testnet (requires DRY_RUN=false)
    #   BINANCE_ENV=live   + EXECUTION_MODE=live   (requires ALL live gates)
    # Everything else is refused before anything can run.
    if execution_mode == "live":
        if binance_env != "live":
            raise ConfigError(
                "refusing EXECUTION_MODE=live with BINANCE_ENV="
                f"{binance_env!r} (live execution requires BINANCE_ENV=live)"
            )
        if dry_run is True or allow_live_execution is False:
            raise ConfigError(
                "refusing EXECUTION_MODE=live: live trading requires "
                "DRY_RUN=false AND ALLOW_LIVE_EXECUTION=true (fail-safe)"
            )
        if not live_api_key or not live_api_secret:
            raise ConfigError("live execution enabled but BINANCE_API_KEY/SECRET are empty")
    elif binance_env == "live":
        raise ConfigError(
            f"refusing BINANCE_ENV=live with EXECUTION_MODE={execution_mode!r} "
            "(BINANCE_ENV=live requires EXECUTION_MODE=live)"
        )

    if execution_mode == "testnet":
        if dry_run is True:
            raise ConfigError(
                "EXECUTION_MODE=testnet requires DRY_RUN=false "
                "(the trading lock must be released explicitly)"
            )
        if not testnet_api_key or not testnet_api_secret:
            raise ConfigError(
                "EXECUTION_MODE=testnet requires testnet credentials "
                "(BINANCE_TESTNET_API_KEY/SECRET)"
            )

    return Config(
        binance_env=binance_env,
        dry_run=bool(dry_run),
        allow_live_execution=bool(allow_live_execution),
        execution_mode=execution_mode,
        pair_list=pair_list,
        indicator_timeframe=indicator_timeframe,
        adx_period=adx_period,
        rsi_period=rsi_period,
        bb_period=bb_period,
        bb_std=bb_std,
        vo_fast=vo_fast,
        vo_slow=vo_slow,
        zscore_period=zscore_period,
        atr_period=atr_period,
        entry_adx_max=entry_adx_max,
        entry_rsi_max=entry_rsi_max,
        entry_volume_osc_min=entry_volume_osc_min,
        entry_bb_percent_b_max=entry_bb_percent_b_max,
        exit_rsi_min=exit_rsi_min,
        exit_adx_min=exit_adx_min,
        exit_bb_percent_b_min=exit_bb_percent_b_min,
        exit_zscore_abs_max=exit_zscore_abs_max,
        grid_step_atr_multiplier=grid_step_atr_multiplier,
        grid_gross_min=grid_gross_min,
        min_net_profit_per_grid=min_net_profit_per_grid,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        slippage_estimate=slippage_estimate,
        max_drawdown_percent=max_drawdown_percent,
        stop_if_below_lower_percent=stop_if_below_lower_percent,
        cooldown_hours=cooldown_hours,
        start_equity=start_equity,
        max_market_data_age_seconds=max_market_data_age_seconds,
        adaptive_grid=bool(adaptive_grid),
        min_grids=min_grids,
        max_grids=max_grids,
        quote_reserve_percent=quote_reserve_percent,
        max_quote_allocation_percent=max_quote_allocation_percent,
        lower_price=lower_price,
        upper_price=upper_price,
        total_grids=total_grids,
        total_quote_budget=total_quote_budget,
        testnet_api_key=testnet_api_key,
        testnet_api_secret=testnet_api_secret,
        live_api_key=live_api_key,
        live_api_secret=live_api_secret,
        env_file=env_file,
    )
