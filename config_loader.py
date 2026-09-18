from __future__ import annotations

from copy import deepcopy
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
            "Live mode is fail-closed: use mode=live, dry_run=false, "
            "and allow_live_execution=true only after live execution is implemented."
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
    range_mode = rng.get("mode")
    if range_mode not in {"auto", "manual"}:
        raise ConfigError("range.mode must be auto or manual")
    if range_mode == "manual":
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

    # Hard safety invariant for this foundation.
    if not dry_run:
        raise ConfigError(
            "This replacement is deliberately dry-run only. "
            "Keep environment.dry_run=true until a separately audited order engine exists."
        )

    # Return a deep copy is unnecessary for validation, but the function is intentionally side-effect free.
    _ = deepcopy(cfg)
