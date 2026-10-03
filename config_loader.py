from __future__ import annotations

from decimal import Decimal, InvalidOperation
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


def _require_positive_int(section: str, key: str, value: Any, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"{section}.{key} must be >= {minimum}")
    return value


def _parse_symbols(cfg: dict[str, Any]) -> list[str]:
    """Parse symbols from config.yaml or environment variable."""
    import os
    symbols_env = os.getenv("SYMBOLS", "").strip()
    if symbols_env:
        symbols = [s.strip().upper() for s in symbols_env.split(",") if s.strip()]
    else:
        symbols_raw = cfg.get("symbols", "")
        if isinstance(symbols_raw, str):
            symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
        elif isinstance(symbols_raw, list):
            symbols = [str(s).strip().upper() for s in symbols_raw if str(s).strip()]
        else:
            symbols = []
    if not symbols:
        raise ConfigError("At least one symbol must be configured (symbols in config.yaml or SYMBOLS env var)")
    for sym in symbols:
        if not sym.isalnum():
            raise ConfigError(f"Invalid symbol: {sym} (must contain only alphanumeric characters)")
    return symbols


def _get_binance_env() -> str:
    """Get Binance environment from environment variable or config."""
    import os
    return os.getenv("BINANCE_ENV", "testnet").strip().lower()


def _load_binance_credentials(env: str) -> dict[str, str]:
    """Load Binance API credentials based on environment (testnet/live)."""
    import os
    if env == "live":
        api_key = os.getenv("BINANCE_LIVE_API_KEY", "")
        api_secret = os.getenv("BINANCE_LIVE_API_SECRET", "")
    else:
        api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    return {"api_key": api_key, "api_secret": api_secret}


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

    # Parse and validate symbols
    symbols = _parse_symbols(cfg)
    cfg["_parsed_symbols"] = symbols  # inject for downstream use

    timeframe = cfg.get("timeframe")
    if timeframe not in ("1h", "4h"):
        raise ConfigError("timeframe must be '1h' or '4h'")

    grid = cfg.get("grid", {})
    mode_by_symbol = grid.get("mode_by_symbol", {})
    if not isinstance(mode_by_symbol, dict):
        raise ConfigError("grid.mode_by_symbol must be a dict mapping symbol -> arithmetic|geometric")
    for sym in symbols:
        if sym not in mode_by_symbol:
            raise ConfigError(f"grid.mode_by_symbol missing entry for {sym}")
        if mode_by_symbol[sym] not in ("arithmetic", "geometric"):
            raise ConfigError(f"grid.mode_by_symbol[{sym}] must be 'arithmetic' or 'geometric'")

    min_gross = _d(grid.get("min_gross_profit_pct", "0.005"))
    hard_min = _d(grid.get("hard_min_net_pct", "0.003"))
    preferred_max = _d(grid.get("preferred_net_max_pct", "0.004"))
    if min_gross <= 0:
        raise ConfigError("grid.min_gross_profit_pct must be > 0")
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

    # Strategy validation
    strategy = cfg.get("strategy", {})
    entry = strategy.get("entry", {})
    exit_cfg = strategy.get("exit", {})
    for key in ("adx_max", "rsi_max", "bb_percent_b_max", "volume_oscillator_min"):
        if key not in entry:
            raise ConfigError(f"strategy.entry.{key} is required")
    for key in ("rsi_min", "adx_min", "bb_percent_b_min", "zscore_threshold"):
        if key not in exit_cfg:
            raise ConfigError(f"strategy.exit.{key} is required")
    # Cooldown hours validation
    cooldown_hours = strategy.get("cooldown_hours")
    if cooldown_hours is None:
        raise ConfigError("strategy.cooldown_hours is required")
    try:
        ch = int(cooldown_hours)
    except (TypeError, ValueError):
        raise ConfigError("strategy.cooldown_hours must be an integer")
    if ch < 0:
        raise ConfigError("strategy.cooldown_hours must be >= 0")

    fees = cfg.get("fees", {})
    for key in ("maker_fee_fallback", "taker_fee_fallback", "slippage_roundtrip_pct"):
        if _d(fees.get(key)) < 0:
            raise ConfigError(f"fees.{key} cannot be negative")

    risk = cfg.get("risk", {})
    if _d(risk.get("max_equity_drawdown_pct")) <= 0:
        raise ConfigError("risk.max_equity_drawdown_pct must be > 0")
    if _d(risk.get("range_break_buffer_pct")) < 0:
        raise ConfigError("risk.range_break_buffer_pct cannot be negative")
    if "stop_if_below_lower_pct" not in risk:
        raise ConfigError(
            "risk.stop_if_below_lower_pct is required "
            "(15m lower-boundary candle-close stop)"
        )
    try:
        stop_pct = Decimal(str(risk["stop_if_below_lower_pct"]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigError(
            "risk.stop_if_below_lower_pct must be a finite decimal strictly "
            "between 0 and 1 (default 0.02)"
        ) from exc
    if not stop_pct.is_finite() or not (Decimal("0") < stop_pct < Decimal("1")):
        raise ConfigError(
            "risk.stop_if_below_lower_pct must be a finite decimal strictly "
            "between 0 and 1 (default 0.02)"
        )

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

    if "adaptive_planner" in cfg:
        _validate_adaptive_planner(cfg)

    if "binance" in cfg:
        _validate_binance(cfg)


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
            "market_intelligence.timeframe must match the configured timeframe"
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
        _require_positive_int("market_intelligence", key, mi.get(key), minimum)

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


def _validate_adaptive_planner(cfg: dict[str, Any]) -> None:
    """Validate the Phase 5A adaptive-planner configuration.

    Missing or malformed values fail closed instead of silently using defaults.
    """
    ap = cfg.get("adaptive_planner")
    if not isinstance(ap, dict):
        raise ConfigError("adaptive_planner section is required")

    _require_positive_int("adaptive_planner", "cooldown_candles",
                          ap.get("cooldown_candles"), 1)

    hyst = ap.get("hysteresis")
    if not isinstance(hyst, dict):
        raise ConfigError("adaptive_planner.hysteresis section is required")

    range_change_pct = _d(hyst.get("range_change_pct"))
    if not range_change_pct.is_finite() or range_change_pct <= 0:
        raise ConfigError("adaptive_planner.hysteresis.range_change_pct must be > 0")

    step_change_pct = _d(hyst.get("step_change_pct"))
    if not step_change_pct.is_finite() or step_change_pct <= 0:
        raise ConfigError("adaptive_planner.hysteresis.step_change_pct must be > 0")

    try:
        grid_count_change = int(hyst.get("grid_count_change"))
    except (TypeError, ValueError):
        raise ConfigError(
            "adaptive_planner.hysteresis.grid_count_change must be an integer >= 0"
        )
    if grid_count_change < 0:
        raise ConfigError("adaptive_planner.hysteresis.grid_count_change must be >= 0")

    quality_degradation = _d(hyst.get("quality_degradation"))
    if not quality_degradation.is_finite() or quality_degradation < 0:
        raise ConfigError(
            "adaptive_planner.hysteresis.quality_degradation must be >= 0"
        )

    regime_change = hyst.get("regime_change")
    if not isinstance(regime_change, bool):
        raise ConfigError(
            "adaptive_planner.hysteresis.regime_change must be boolean"
        )


def _validate_binance(cfg: dict[str, Any]) -> None:
    """Validate the Binance adapter configuration for both testnet and live."""
    bn = cfg.get("binance")
    if not isinstance(bn, dict):
        raise ConfigError("binance section is required")

    # Validate testnet section
    testnet = bn.get("testnet")
    if not isinstance(testnet, dict):
        raise ConfigError("binance.testnet section is required")
    env = testnet.get("environment")
    if env != "testnet":
        raise ConfigError(f"binance.testnet.environment must be 'testnet' (got {env!r})")
    base_url = str(testnet.get("base_url", "")).strip()
    if not base_url:
        raise ConfigError("binance.testnet.base_url is required")
    if "testnet.binance.vision" not in base_url:
        raise ConfigError(
            "binance.testnet.base_url must point to the Binance Spot Testnet endpoint. "
            "Production endpoints are not permitted."
        )

    # Validate live section
    live = bn.get("live")
    if not isinstance(live, dict):
        raise ConfigError("binance.live section is required")
    env = live.get("environment")
    if env != "live":
        raise ConfigError(f"binance.live.environment must be 'live' (got {env!r})")
    base_url = str(live.get("base_url", "")).strip()
    if not base_url:
        raise ConfigError("binance.live.base_url is required")
    if "api.binance.com" not in base_url:
        raise ConfigError(
            "binance.live.base_url must point to the Binance Spot production endpoint."
        )

    for section_name in ("testnet", "live"):
        section = bn.get(section_name, {})
        for key, minimum in (
            ("timeout_ms", 1),
            ("retries", 0),
            ("backoff_ms", 0),
        ):
            _require_positive_int(f"binance.{section_name}", key, section.get(key), minimum)

        timeout = int(section.get("timeout_ms", 0))
        if timeout > 30000:
            raise ConfigError(f"binance.{section_name}.timeout_ms must not exceed 30000 (30s)")

        for key in ("max_open_orders", "max_account_assets"):
            if key in section:
                value = section.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(
                        f"binance.{section_name}.{key}, when provided, must be an integer >= 1"
                    )