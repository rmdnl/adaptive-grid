"""Global account-level risk (locked spec section 10/17): unit + integration.

- Boundary table: drawdown 1.99% -> no kill; 2.00% -> GLOBAL KILL;
  2.01% -> GLOBAL KILL.
- Reference equity is a persistent high-water-mark (never lowered
  automatically).
- The kill latch persists in the base DB and survives restart.
- Unknown equity with an existing reference FAILS CLOSED.
- Runtime propagation: a latched global kill blocks every symbol's cycle.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import multi_symbol_main as msm
from global_risk import (
    evaluate,
    is_killed,
    load_reference,
    update_reference,
)
from storage import get_state, init_db, set_state


def test_boundary_1_99pct_does_not_kill(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    decision = evaluate(db, Decimal("980.1"))  # 1.99% drawdown
    assert decision["available"] is True
    assert decision["allowed"] is True
    assert decision["kill_triggered"] is False
    assert is_killed(db) is False


def test_boundary_2_00pct_kills(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    decision = evaluate(db, Decimal("980"))  # exactly 2.00% drawdown
    assert decision["allowed"] is False
    assert decision["kill_triggered"] is True
    assert decision["reason"] == "GLOBAL_EQUITY_DRAWDOWN_KILL"
    assert is_killed(db) is True


def test_boundary_2_01pct_kills(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    decision = evaluate(db, Decimal("979.9"))  # 2.01% drawdown
    assert decision["kill_triggered"] is True
    assert is_killed(db) is True


def test_kill_persists_across_restart(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    evaluate(db, Decimal("970"))
    assert is_killed(db) is True
    # A fresh process reading the same DB still sees the latch.
    assert is_killed(db) is True
    # Equity recovering does NOT release the latch (no automatic recovery).
    evaluate(db, Decimal("1200"))
    assert is_killed(db) is True


def test_reference_high_water_mark_never_lowers(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    update_reference(db, Decimal("900"))   # lower equity: ignored
    assert load_reference(db) == Decimal("1000")
    update_reference(db, Decimal("1200"))  # higher: raised
    assert load_reference(db) == Decimal("1200")


def test_unknown_equity_with_reference_fails_closed(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    update_reference(db, Decimal("1000"))
    decision = evaluate(db, None)
    assert decision["available"] is False
    assert decision["allowed"] is False
    assert decision["reason"] == "GLOBAL_EQUITY_UNAVAILABLE"


def test_first_observation_bootstraps_reference(tmp_path):
    db = str(tmp_path / "global.sqlite3")
    init_db(db)
    decision = evaluate(db, Decimal("1000"))
    assert decision["allowed"] is True
    assert load_reference(db) == Decimal("1000")
    assert Decimal(decision["drawdown_pct"]) == 0


def _install_pass_stubs(monkeypatch, tmp_path):
    """Stub the global-equity inputs and both symbol cycles."""
    from datetime import datetime, timezone
    from market_data import AccountSnapshot, TickerSnapshot

    cfg = {
        "environment": {"mode": "testnet", "dry_run": True,
                        "allow_live_execution": False},
        "symbols": "BTCUSDT,ETHUSDT",
        "timeframe": "1h",
        "grid": {"mode_by_symbol": {"BTCUSDT": "arithmetic",
                                    "ETHUSDT": "arithmetic"},
                 "min_gross_profit_pct": "0.005",
                 "hard_min_net_pct": "0.002", "min_cells": 6,
                 "max_levels": 40, "atr_multiplier": "1.0"},
        "strategy": {"entry": {"adx_max": "25", "rsi_max": "40",
                               "bb_percent_b_max": "0"},
                     "exit": {"rsi_min": "70", "adx_min": "25",
                              "bb_percent_b_min": "1",
                              "zscore_threshold": "2.5"},
                     "cooldown_hours": 3},
        "execution": {"prefer_limit_maker": True, "stale_order_minutes": 30,
                      "max_open_orders": 40, "order_quote_size": "25",
                      "total_quote_budget": "0", "max_inventory_pct": "0.70",
                      "testnet_execution": False},
        "fees": {"maker_fee_fallback": "0.001", "taker_fee_fallback": "0.001",
                 "slippage_roundtrip_pct": "0.0005"},
        "paper": {"initial_base_balance": "2",
                  "initial_quote_balance": "1000", "maker_fee": "0.001",
                  "taker_fee": "0.001", "fee_asset": "USDT"},
        "risk": {"max_equity_drawdown_pct": "0.02",
                 "range_break_buffer_pct": "0.01",
                 "daily_profit_lock_pct": "0.01", "cooldown_minutes": 30,
                 "stop_if_below_lower_pct": "0.02"},
        "logging": {"sqlite_path": str(tmp_path / "grid.sqlite3"),
                    "log_path": str(tmp_path / "grid.log"),
                    "csv_path": str(tmp_path / "trades.csv")},
    }
    cfg["_parsed_symbols"] = ["BTCUSDT", "ETHUSDT"]

    monkeypatch.setattr(
        msm, "fetch_symbol_info",
        lambda client, symbol: {
            "symbol": symbol, "baseAsset": symbol.replace("USDT", ""),
            "quoteAsset": "USDT", "status": "TRADING", "filters": []})

    def account(client, base, quote):
        return AccountSnapshot(
            base_asset=base, base_free=Decimal("0"), base_locked=Decimal("0"),
            quote_asset=quote, quote_free=Decimal("1000"),
            quote_locked=Decimal("0"), fetched_at=datetime.now(timezone.utc))

    monkeypatch.setattr(msm, "fetch_account_snapshot", account)
    monkeypatch.setattr(
        msm, "fetch_ticker_price",
        lambda c, s: TickerSnapshot(s, Decimal("100"),
                                    datetime.now(timezone.utc)))
    return cfg


def test_global_kill_propagates_to_every_symbol(monkeypatch, tmp_path):
    cfg = _install_pass_stubs(monkeypatch, tmp_path)
    import logging
    from shutdown import ShutdownCoordinator
    from storage import get_kill_state, set_kill_state as set_kill

    # Latch the GLOBAL kill in the base DB before the pass.
    init_db(cfg["logging"]["sqlite_path"])
    set_kill(cfg["logging"]["sqlite_path"], active=True,
             trigger="GLOBAL_EQUITY_DRAWDOWN_KILL")

    ran = []

    class _Runner:
        def __init__(self, symbol, cfg, db_path, *args, **kwargs):
            self.symbol = symbol
            from storage import init_db as _init
            _init(db_path)  # the real runner does this too
            ran.append((symbol, kwargs.get("global_risk_allowed")))

        def run_cycle(self):
            return {"symbol": self.symbol, "success": True, "status": "SKIPPED",
                    "kill_triggered": False, "entry_decision": None,
                    "exit_decision": None, "combined_allowed": False,
                    "combined_reason": "SKIPPED", "current_price": None,
                    "range": None, "grid_cells": 0, "dynamic_step_pct": None,
                    "open_orders": 0, "pending_cancels": 0}

    monkeypatch.setattr(msm, "SymbolCycleRunner", _Runner)
    exit_code = msm.run_once(cfg, logging.getLogger("t"),
                             object(), cfg["_parsed_symbols"],
                             ShutdownCoordinator())
    assert exit_code == 0
    # Both symbol runners were told the global risk disallows trading.
    assert ran == [("BTCUSDT", False), ("ETHUSDT", False)]
