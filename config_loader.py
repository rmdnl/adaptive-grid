from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ALLOWED_MODES = {"testnet", "live"}

class ConfigError(ValueError):
    pass

def load_config(path: str = "config.yaml") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    validate_config(cfg)
    return cfg

def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ConfigError(f"Invalid decimal value: {value!r}") from exc

def validate_config(cfg: dict[str, Any]) -> None:
    env = cfg.get("environment", {})
    mode = env.get("mode")
    dry_run = env.get("dry_run")
    allow_live = env.get("allow_live_execution")

    if mode not in ALLOWED_MODES:
        raise ConfigError(f"environment.mode must be one of {sorted(ALLOWED_MODES)}")
    if not isinstance(dry_run, bool):
        raise ConfigError("environment.dry_run must be boolean")
    if not isinstance(allow_live, bool):
        raise ConfigError("environment.allow_live_execution must be boolean")
    if mode == "live" and (dry_run or not allow_live):
        raise ConfigError(
            "Live mode is fail-closed: live execution is not implemented in this release."
        )

    symbol = str(cfg.get("symbol", "")).upper().strip()
    if not symbol.isalnum():
        raise ConfigError("symbol must contain only Binance symbol characters")
    if cfg.get("timeframe") != "15m":
        raise ConfigError("This safety foundation is locked to 15m")

    grid = cfg.get("grid", {})
    step = _d(grid.get("step_pct"))
    hard_min = _d(grid.get("hard_min_net_pct"))
    preferred_max = _d(grid.get("preferred_net_max_pct"))
    if step <= 0:
        raise ConfigError("grid.step_pct must be > 0")
    if hard_min < _d("0.003"):
        raise ConfigError("grid.hard_min_net_pct cannot be below 0.003")
    if preferred_max < hard_min:
        raise ConfigError("grid.preferred_net_max_pct must be >= hard_min_net_pct")
    min_cells = int(grid.get("min_cells", 0))
    max_levels = int(grid.get("max_levels", 0))
    if min_cells < 1 or max_levels <= min_cells:
        raise ConfigError("grid min_cells/max_levels are invalid")

    rng = cfg.get("range", {})
    if rng.get("mode") not in {"auto", "manual"}:
        raise ConfigError("range.mode must be auto or manual")
    if rng.get("mode") == "manual":
        lo = _d(rng.get("lower_price"))
        hi = _d(rng.get("upper_price"))
        if lo <= 0 or hi <= lo:
            raise ConfigError("Manual range requires 0 < lower_price < upper_price")

    fees = cfg.get("fees", {})
    for key in ("maker_fee_fallback", "taker_fee_fallback", "slippage_roundtrip_pct"):
        if _d(fees.get(key)) < 0:
            raise ConfigError(f"fees.{key} cannot be negative")

    risk = cfg.get("risk", {})
    if _d(risk.get("max_equity_drawdown_pct")) <= 0:
        raise ConfigError("risk.max_equity_drawdown_pct must be > 0")
    if _d(risk.get("range_break_buffer_pct")) < 0:
        raise ConfigError("risk.range_break_buffer_pct cannot be negative")

    execution = cfg.get("execution", {})
    if int(execution.get("max_open_orders", 0)) < 1:
        raise ConfigError("execution.max_open_orders must be >= 1")
    if _d(execution.get("order_quote_size")) <= 0:
        raise ConfigError("execution.order_quote_size must be > 0")

    paper = cfg.get("paper", {})
    for required_key in (
        "initial_base_balance",
        "initial_quote_balance",
        "maker_fee",
        "taker_fee",
        "fee_asset",
    ):
        if required_key not in paper:
            raise ConfigError(f"paper.{required_key} is required")
    initial_base = _d(paper.get("initial_base_balance"))
    initial_quote = _d(paper.get("initial_quote_balance"))
    if initial_base < 0:
        raise ConfigError("paper.initial_base_balance cannot be negative")
    if initial_quote < 0:
        raise ConfigError("paper.initial_quote_balance cannot be negative")
    maker_fee = _d(paper.get("maker_fee"))
    taker_fee = _d(paper.get("taker_fee"))
    if maker_fee < 0:
        raise ConfigError("paper.maker_fee cannot be negative")
    if taker_fee < 0:
        raise ConfigError("paper.taker_fee cannot be negative")
    fee_asset = str(paper.get("fee_asset", "")).upper().strip()
    if not fee_asset:
        raise ConfigError("paper.fee_asset is required")

    if not dry_run:
        raise ConfigError(
            "This replacement is deliberately dry-run only. "
            "Keep environment.dry_run=true until a separately audited order engine exists."
        )

    if "market_intelligence" in cfg:
        _validate_market_intelligence(cfg)

def _require_positive_int(section: str, key: str, value: Any, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"market_intelligence.{section}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"market_intelligence.{section}.{key} must be >= {minimum}")
    return value

def _validate_market_intelligence(cfg: dict[str, Any]) -> None:
    """Validate the Phase 4 read-only market-intelligence configuration.

    Every threshold is explicit, named and validated. Missing or malformed
    values fail closed instead of silently substituting defaults.
    """
    mi = cfg.get("market_intelligence")
    if not isinstance(mi, dict):
        raise ConfigError("market_intelligence section is required")

    timeframe = mi.get("timeframe")
    if timeframe != cfg.get("timeframe"):
        raise ConfigError(
            "market_intelligence.timeframe must match the locked 15m timeframe"
        )

    for key, minimum in (
        ("min_candles", 2),
        ("max_candle_age_seconds", 1),
        ("atr_period", 1),
        ("adx_period", 1),
        ("bb_length", 2),
        ("volume_baseline_period", 1),
        ("range_stability_period", 2),
    ):
        _require_positive_int("_root", key, mi.get(key), minimum)

    if len({mi["atr_period"], mi["adx_period"], mi["bb_length"]}) < 1:
        raise ConfigError("market_intelligence indicator periods must be positive")
    bb_len = mi["bb_length"]
    if mi["adx_period"] * 2 > mi["min_candles"]:
        raise ConfigError("market_intelligence.min_candles is too small for the ADX period")
    if bb_len > mi["min_candles"]:
        raise ConfigError("market_intelligence.min_candles is too small for the Bollinger length")

    bb_mult = _d(mi.get("bb_std_mult"))
    if not bb_mult.is_finite() or bb_mult <= 0:
        raise ConfigError("market_intelligence.bb_std_mult must be > 0")

    regime = mi.get("regime")
    if not isinstance(regime, dict):
        raise ConfigError("market_intelligence.regime section is required")
    adx_trend_min = _d(regime.get("adx_trend_min"))
    if not adx_trend_min.is_finite() or not (_d("0") <= adx_trend_min <= _d("100")):
        raise ConfigError("market_intelligence.regime.adx_trend_min must be within 0..100")
    atr_expansion = _d(regime.get("atr_expansion_ratio"))
    if not atr_expansion.is_finite() or atr_expansion <= 0:
        raise ConfigError("market_intelligence.regime.atr_expansion_ratio must be > 0")
    inclusion = _d(regime.get("price_range_inclusion_min"))
    if not inclusion.is_finite() or not (_d("0") <= inclusion <= _d("1")):
        raise ConfigError(
            "market_intelligence.regime.price_range_inclusion_min must be within 0..1"
        )
    efficiency = _d(regime.get("directional_efficiency_max"))
    if not efficiency.is_finite() or not (_d("0") <= efficiency <= _d("1")):
        raise ConfigError(
            "market_intelligence.regime.directional_efficiency_max must be within 0..1"
        )

    liquidity = mi.get("liquidity")
    if not isinstance(liquidity, dict):
        raise ConfigError("market_intelligence.liquidity section is required")
    max_spread = _d(liquidity.get("max_spread_pct"))
    if not max_spread.is_finite() or max_spread <= 0:
        raise ConfigError("market_intelligence.liquidity.max_spread_pct must be > 0")
    _require_positive_int(
        "liquidity", "max_quote_ticker_age_seconds",
        liquidity.get("max_quote_ticker_age_seconds"), 1,
    )

    quality = mi.get("quality")
    if not isinstance(quality, dict):
        raise ConfigError("market_intelligence.quality section is required")
    min_score = _d(quality.get("min_range_quality_score"))
    if not min_score.is_finite() or not (_d("0") <= min_score <= _d("100")):
        raise ConfigError(
            "market_intelligence.quality.min_range_quality_score must be within 0..100"
        )
    weight_keys = (
        "weight_trend_stability",
        "weight_volatility_suitability",
        "weight_bb_width_suitability",
        "weight_volume_stability",
        "weight_spread_suitability",
        "weight_range_containment",
    )
    weights = []
    for key in weight_keys:
        if key not in quality:
            raise ConfigError(f"market_intelligence.quality.{key} is required")
        weight = _d(quality.get(key))
        if not weight.is_finite() or weight < 0:
            raise ConfigError(f"market_intelligence.quality.{key} must be >= 0")
        weights.append(weight)
    if sum(weights) != _d("1"):
        raise ConfigError("market_intelligence.quality weights must sum exactly to 1")
