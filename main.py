from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from config_loader import ConfigError, load_config
from grid_engine import build_geometric_grid, validate_grid_profit
from indicators import enrich, latest_valid_row
from market_data import (
    fetch_account_commission,
    fetch_klines,
    fetch_symbol_info,
    make_client,
)
from profit_model import profit_class
from range_engine import auto_range
from risk_engine import (
    combine,
    cooldown_gate,
    daily_profit_lock,
    equity_dd_kill,
    inventory_gate,
    market_gate,
    open_orders_gate,
    profit_gate,
    range_gate,
)
from storage import init_db, record_risk_event, set_state
from symbol_rules import parse_symbol_info


def _logger(path: str) -> logging.Logger:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("adaptive_grid")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger


def _commission_rates(payload: dict | None, fallback_maker, fallback_taker) -> tuple[Decimal, Decimal]:
    maker = Decimal(str(fallback_maker))
    taker = Decimal(str(fallback_taker))
    if not payload:
        return maker, taker

    # The generated SDK model may expose these under different nesting depending on version.
    candidates = [
        payload.get("standardCommissionForOrder"),
        payload.get("standardCommissionForOrderMaker"),
    ]
    for item in candidates:
        if isinstance(item, dict):
            values = item.get("maker") or item.get("makerCommission")
            if isinstance(values, list) and values:
                maker = Decimal(str(values[0]))
            elif values is not None:
                maker = Decimal(str(values))

            values = item.get("taker") or item.get("takerCommission")
            if isinstance(values, list) and values:
                taker = Decimal(str(values[0]))
            elif values is not None:
                taker = Decimal(str(values))
    return maker, taker


def main() -> int:
    load_dotenv()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG BLOCK: {exc}")
        return 2

    db_path = cfg["logging"]["sqlite_path"]
    init_db(db_path)
    logger = _logger(cfg["logging"]["log_path"])

    mode = cfg["environment"]["mode"]
    symbol = cfg["symbol"]
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    # This foundation is intentionally public-market-data + dry-run only.
    client = make_client(mode, api_key, api_secret)

    symbol_info = fetch_symbol_info(client, symbol)
    rules = parse_symbol_info(symbol_info)

    df = fetch_klines(
        client,
        symbol,
        cfg["timeframe"],
        cfg["range"]["lookback"],
        drop_incomplete=True,
    )
    enriched = enrich(df)
    last = latest_valid_row(enriched)

    commission_payload, fee_source = fetch_account_commission(client, symbol)
    maker_fee, taker_fee = _commission_rates(
        commission_payload,
        cfg["fees"]["maker_fee_fallback"],
        cfg["fees"]["taker_fee_fallback"],
    )

    if cfg["range"]["mode"] == "manual":
        lower = Decimal(str(cfg["range"]["lower_price"]))
        upper = Decimal(str(cfg["range"]["upper_price"]))
        range_quality = 100.0
        range_reason = "MANUAL_RANGE"
        range_approved = True
        position_in_range = float(
            (Decimal(str(last["close"])) - lower) / (upper - lower)
        )
    else:
        candidate = auto_range(enriched, **cfg["range"]["auto"])
        lower = candidate.lower
        upper = candidate.upper
        range_quality = candidate.quality
        range_reason = candidate.reason
        range_approved = candidate.approved
        position_in_range = candidate.position_in_range

    current_price = Decimal(str(last["close"]))

    # A malformed/invalid range must block before any grid construction.
    levels = []
    effective_upper = upper
    if lower <= 0 or upper <= lower:
        grid_allowed = False
        grid_reason = "INVALID_RANGE"
        validation = None
    else:
        try:
            levels, effective_upper = build_geometric_grid(
                lower,
                upper,
                cfg["grid"]["step_pct"],
                min_cells=int(cfg["grid"]["min_cells"]),
                max_levels=int(cfg["grid"]["max_levels"]),
            )
            validation = validate_grid_profit(
                levels,
                maker_fee,
                maker_fee if cfg["execution"]["prefer_limit_maker"] else taker_fee,
                cfg["fees"]["slippage_roundtrip_pct"],
                cfg["grid"]["hard_min_net_pct"],
            )
            grid_allowed = validation.allowed
            grid_reason = validation.reason
        except (ValueError, ArithmeticError) as exc:
            grid_allowed = False
            grid_reason = f"GRID_BUILD_BLOCK:{exc}"
            validation = None

    decisions = [
        profit_gate(
            validation.min_net_pct if validation is not None else Decimal("0"),
            cfg["grid"]["hard_min_net_pct"],
        ),
        market_gate(last, cfg["market_filter"]),
        range_gate(lower, upper, current_price, cfg["risk"]["range_break_buffer_pct"]),
        equity_dd_kill(Decimal("0"), cfg["risk"]["max_equity_drawdown_pct"]),
        inventory_gate(Decimal("0"), cfg["execution"]["max_inventory_pct"]),
        open_orders_gate(0, cfg["execution"]["max_open_orders"]),
        cooldown_gate(False),
        daily_profit_lock(Decimal("0"), cfg["risk"]["daily_profit_lock_pct"]),
    ]
    combined = combine(*decisions)

    if not range_approved:
        combined = combine(combined, type(combined)(False, (f"RANGE:{range_reason}",)))
    if not grid_allowed:
        combined = combine(combined, type(combined)(False, (f"GRID:{grid_reason}",)))

    record_risk_event(
        db_path,
        combined.allowed,
        combined.reason,
        {
            "symbol": symbol,
            "price": str(current_price),
            "range": [str(lower), str(upper)],
            "range_quality": range_quality,
            "grid_levels": len(levels),
            "min_net_pct": str(validation.min_net_pct if validation is not None else Decimal("0")),
            "grid_reason": grid_reason,
        },
    )

    set_state(db_path, "last_symbol", symbol)
    set_state(db_path, "last_price", str(current_price))
    set_state(db_path, "last_range", {"lower": str(lower), "upper": str(effective_upper)})
    set_state(db_path, "last_risk_decision", {
        "allowed": combined.allowed,
        "reason": combined.reason,
    })

    logger.info("=== Adaptive Grid v3.2 Safety Foundation ===")
    logger.info("Mode=%s dry_run=%s symbol=%s", mode, cfg["environment"]["dry_run"], symbol)
    logger.info(
        "Price=%s Range=%s -> %s Quality=%.2f Reason=%s Position=%.2f",
        current_price, lower, upper, range_quality, range_reason, position_in_range,
    )
    logger.info(
        "Fees maker=%s taker=%s source=%s | Grid step=%.3f%% cells=%d effective_upper=%s",
        maker_fee, taker_fee, fee_source, float(cfg["grid"]["step_pct"]) * 100,
        validation.cells, effective_upper,
    )
    logger.info(
        "Indicators ADX=%.2f ATR=%.3f%% BB=%.3f%% Vol=%.2fx RSI=%.2f",
        float(last["adx"]),
        float(last["atr_pct"]) * 100,
        float(last["bb_width"]) * 100,
        float(last["volume_ratio"]),
        float(last["rsi"]),
    )
    logger.info(
        "Net/grid=%.4f%% class=%s grid=%s market/risk=%s",
        float(validation.min_net_pct if validation is not None else Decimal("0")) * 100,
        profit_class(
            validation.min_net_pct if validation is not None else Decimal("0"),
            cfg["grid"]["hard_min_net_pct"],
            cfg["grid"]["preferred_net_max_pct"],
        ),
        grid_reason,
        combined.reason,
    )
    logger.info(
        "Symbol rules tick=%s step=%s minQty=%s minNotional=%s",
        rules.tick_size, rules.step_size, rules.min_qty, rules.min_notional,
    )

    if not combined.allowed:
        logger.warning("ORDER PLAN BLOCKED: %s", combined.reason)
    else:
        logger.info("ORDER PLAN PASS: dry-run only, no order is submitted.")

    print("\nResult:")
    print(f"  Risk decision : {'PASS' if combined.allowed else 'BLOCK'}")
    print(f"  Reason        : {combined.reason}")
    print(f"  Price         : {current_price}")
    print(f"  Range         : {lower} -> {effective_upper}")
    print(f"  Grid cells    : {validation.cells if validation is not None else 0}")
    print(f"  Net/grid      : {(validation.min_net_pct if validation is not None else Decimal('0')) * 100:.4f}%")
    print(f"  Range quality : {range_quality:.2f}/100")
    print(f"  Fee source    : {fee_source}")
    print("  Execution     : DRY RUN, no order placement")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
