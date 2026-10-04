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

    # Equity anchor for the drawdown kill switch: the PnL-tracking model
    # measures equity as start_equity + realized - fees + unrealized, so
    # the 2% drawdown limit is a percentage of a real capital base instead
    # of a near-zero PnL peak (which would trip on fee-sized noise).
    start_equity: float

    testnet_api_key: str
    testnet_api_secret: str
    live_api_key: str
    live_api_secret: str

    env_file: str

    @property
    def allow_live(self) -> bool:
        """Live endpoints/credentials are used only when ALL three gates
        are explicitly satisfied: DRY_RUN=false AND ALLOW_LIVE_EXECUTION=true
        AND BINANCE_ENV=live."""
        return (
            (not self.dry_run)
            and self.allow_live_execution
            and self.binance_env == "live"
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


def _get_int(env: dict, key: str, minimum: Optional[int], errors: list) -> Optional[int]:
    if _missing(env, key):
        errors.append(f"missing required key: {key}")
        return None
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

    start_equity = _get_float(env, "START_EQUITY", default=1000.0, minimum=0.000001, maximum=None, errors=errors)

    testnet_api_key = str(env.get("BINANCE_TESTNET_API_KEY") or "").strip()
    testnet_api_secret = str(env.get("BINANCE_TESTNET_API_SECRET") or "").strip()
    live_api_key = str(env.get("BINANCE_LIVE_API_KEY") or "").strip()
    live_api_secret = str(env.get("BINANCE_LIVE_API_SECRET") or "").strip()

    if errors:
        raise ConfigError("invalid configuration:\n- " + "\n- ".join(errors))

    # Live-safety gates: refuse dangerous or contradictory combinations
    # before anything can run.
    if binance_env == "live" and dry_run is False and allow_live_execution is False:
        raise ConfigError(
            "refusing BINANCE_ENV=live with DRY_RUN=false and ALLOW_LIVE_EXECUTION=false "
            "(fail-safe; live execution requires the explicit gate)"
        )

    allow_live = bool(
        dry_run is False and allow_live_execution is True and binance_env == "live"
    )
    if allow_live and (not live_api_key or not live_api_secret):
        raise ConfigError("live execution enabled but BINANCE_LIVE_API_KEY/SECRET are empty")

    if dry_run is False and not allow_live and (not testnet_api_key or not testnet_api_secret):
        raise ConfigError(
            "DRY_RUN=false requires testnet credentials (BINANCE_TESTNET_API_KEY/SECRET) "
            "unless all live gates are satisfied"
        )

    return Config(
        binance_env=binance_env,
        dry_run=bool(dry_run),
        allow_live_execution=bool(allow_live_execution),
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
        testnet_api_key=testnet_api_key,
        testnet_api_secret=testnet_api_secret,
        live_api_key=live_api_key,
        live_api_secret=live_api_secret,
        env_file=env_file,
    )
