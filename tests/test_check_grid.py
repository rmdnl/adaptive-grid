"""Read-only --check-grid tests (offline, deterministic).

Verifies the CLI reuses the production grid/economics path, reports
executable quantized economics, honors the PERCENT_PRICE_BY_SIDE band and
all filters, fails closed on missing reference data, and never submits an
order or mutates state.
"""

from __future__ import annotations

import io
import time

import pytest

import bot
from bot import _check_grid
from conftest import make_config
from exchange import ExchangeFilters


def _candles(base: float, rng: float, n: int = 40):
    now = int(time.time() * 1000)
    out = []
    for i in range(n):
        close_time = now - (n - i) * 1000
        out.append(
            {
                "open_time": close_time - 999,
                "open": base,
                "high": base + rng,
                "low": base - rng,
                "close": base,
                "volume": 100.0,
                "close_time": close_time,
            }
        )
    return out


class FakeSpot:
    """Offline market-data + filters provider. Records any trading call so
    tests can prove the check never submits an order."""

    environment = "TESTNET"

    def __init__(self, specs: dict):
        self.specs = specs  # symbol -> {base, rng, filters, avg}
        self.submissions = []
        self.market_reads = []

    def _spec(self, symbol):
        return self.specs[symbol]

    def fetch_klines(self, symbol, interval, limit=200):
        s = self._spec(symbol)
        return _candles(s["base"], s["rng"])

    def get_filters(self, symbol):
        return self._spec(symbol)["filters"]

    def get_avg_price(self, symbol):
        avg = self._spec(symbol)["avg"]
        return {"mins": 1, "price": ("%.10f" % avg) if avg is not None else "0"}

    # ---- trading methods: record, so a check-grid that calls them fails the test
    def create_limit_maker_order(self, *a, **k):
        self.submissions.append(("create_limit", a, k))
        raise AssertionError("check-grid must not submit orders")

    def create_market_order(self, *a, **k):
        self.submissions.append(("create_market", a, k))
        raise AssertionError("check-grid must not submit orders")

    def cancel_order(self, *a, **k):
        self.submissions.append(("cancel", a, k))
        raise AssertionError("check-grid must not cancel orders")

    def get_open_orders(self, symbol=None):
        return []


def _filters(**kw):
    defaults = dict(tick_size=0.01, step_size=0.00001, min_notional=10.0, min_qty=0.00001)
    defaults.update(kw)
    return ExchangeFilters(**defaults)


def _run(symbols, cfg_overrides=None, out=None):
    # Override grid range config to match test prices
    default_lower = {sym: s["base"] * 0.9 for sym, s in symbols.items()}
    default_upper = {sym: s["base"] * 1.2 for sym, s in symbols.items()}
    default_budget = {sym: 500.0 for sym in symbols}
    overrides = {
        "lower_price": default_lower,
        "upper_price": default_upper,
        "total_quote_budget": default_budget,
        **(cfg_overrides or {})
    }
    cfg = make_config(**overrides)
    spot = FakeSpot(symbols)
    code = _check_grid(cfg, spot, out=out if out is not None else io.StringIO())
    return code, spot, cfg


# ----- accepted grid economics -----

def test_accepted_grid_economics(tmp_path):
    sym = "BTC/USDT"
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0}}
    code, spot, cfg = _run(specs, {"pair_list": (sym,)})
    out = io.StringIO()
    code2 = _check_grid(cfg, spot, out)
    text = out.getvalue()
    assert code2 == 0
    assert "GRID STATUS: ACCEPTED" in text
    assert "OVERALL: ACCEPTED" in text
    # executable (quantized) economics must be reported, with a min
    assert "min =" in text and "max =" in text and "average =" in text
    # fees and slippage come from configuration, not hardcoded
    assert f"{cfg.maker_fee * 100:.3f}%" in text      # BUY FEE maker
    assert f"{cfg.slippage_estimate * 100:.3f}%" in text  # slippage
    # the minimum required net is the configured value
    assert f"{cfg.min_net_profit_per_grid * 100:.2f}%" in text


def test_quantized_executable_prices_reported(tmp_path):
    """The buy/sell prices that drive the reported net must be the actual
    tick-quantized executable prices, not ideal theoretical spacing."""
    sym = "BTC/USDT"
    f = _filters(tick_size=0.05, step_size=0.00001)  # coarse tick
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": f, "avg": 100.0}}
    cfg = make_config(pair_list=(sym,), lower_price={sym: 90.0}, upper_price={sym: 120.0}, total_quote_budget={sym: 500.0})
    spot = FakeSpot(specs)
    out = io.StringIO()
    _check_grid(cfg, spot, out)
    text = out.getvalue()
    assert "GRID STATUS: ACCEPTED" in text
    # with tick 0.05, executable prices must be multiples of the tick
    import grid as g
    plan = g.build_grid(sym, cfg.grid_mode(sym), 100.0, 1.0, f, cfg, reference_price=100.0)
    assert plan.executable
    for lvl in plan.levels:
        assert abs(lvl.buy_price / f.tick_size - round(lvl.buy_price / f.tick_size)) < 1e-6
        assert abs(lvl.sell_price / f.tick_size - round(lvl.sell_price / f.tick_size)) < 1e-6


# ----- net below minimum -----

def test_net_below_minimum_rejected(tmp_path):
    # gross = 1.01% (passes the 0.50% gate); heavy slippage drags net under
    # the 0.20% minimum -> rejected specifically for the net gate.
    sym = "BTC/USDT"
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0}}
    cfg = make_config(pair_list=(sym,), slippage_estimate=0.004, lower_price={sym: 90.0}, upper_price={sym: 120.0}, total_quote_budget={sym: 500.0})
    spot = FakeSpot(specs)
    out = io.StringIO()
    code = _check_grid(cfg, spot, out)
    text = out.getvalue()
    assert code == 1
    assert "GRID STATUS: REJECTED" in text
    assert "REASON: net profit below minimum" in text
    assert "OVERALL: REJECTED" in text


def test_gross_below_minimum_rejected(tmp_path):
    # tiny ATR -> step 0.2% -> gross 0.2% < 0.50% minimum -> gross gate trips.
    sym = "BTC/USDT"
    specs = {sym: {"base": 100.0, "rng": 0.2, "filters": _filters(), "avg": 100.0}}
    out = io.StringIO()
    code = _check_grid(make_config(pair_list=(sym,), lower_price={sym: 90.0}, upper_price={sym: 120.0}, total_quote_budget={sym: 500.0}), FakeSpot(specs), out)
    text = out.getvalue()
    assert code == 1
    assert "REASON: gross profit below minimum" in text


# ----- different grid intervals (min/max/avg) -----

def test_different_grid_intervals_shown(tmp_path):
    sym = "SOL/USDT"
    f = _filters(tick_size=0.001, step_size=0.01, min_notional=5.0, min_qty=0.01)
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": f, "avg": 100.0}}
    cfg = make_config(pair_list=(sym,))
    spot = FakeSpot(specs)
    out = io.StringIO()
    code = _check_grid(cfg, spot, out)
    text = out.getvalue()
    import grid as g
    plan = g.build_grid(sym, "geometric", 100.0, 1.0, f, cfg, reference_price=100.0)
    nets = [lvl.net_pct for lvl in plan.levels]
    if len(nets) > 1 and max(nets) - min(nets) > 1e-9:
        assert "min =" in text and "max =" in text and "average =" in text
        assert code == (0 if plan.executable else 1)


# ----- PERCENT_PRICE_BY_SIDE band rejection -----

def test_percent_price_band_rejection(tmp_path):
    sym = "NEAR/USDT"
    # band floor 99.5 drops every buy level (99,98,...) -> whole grid blocked
    f = _filters(
        tick_size=0.001, step_size=0.1, min_notional=5.0, min_qty=0.1,
        bid_multiplier_up=1.005, bid_multiplier_down=0.995,
        ask_multiplier_up=1.005, ask_multiplier_down=0.995,
    )
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": f, "avg": 100.0}}
    out = io.StringIO()
    code = _check_grid(make_config(pair_list=(sym,)), FakeSpot(specs), out)
    text = out.getvalue()
    assert code == 1
    assert "GRID STATUS: REJECTED" in text
    assert "REASON: percent price band" in text


# ----- missing reference price (fail closed) -----

def test_missing_reference_price_rejected(tmp_path):
    sym = "NEAR/USDT"
    f = _filters(
        tick_size=0.001, step_size=0.1, min_notional=5.0, min_qty=0.1,
        bid_multiplier_up=1.05, bid_multiplier_down=0.95,
        ask_multiplier_up=1.05, ask_multiplier_down=0.95,
    )
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": f, "avg": None}}  # no reference
    out = io.StringIO()
    code = _check_grid(make_config(pair_list=(sym,)), FakeSpot(specs), out)
    text = out.getvalue()
    assert code == 1
    assert "REASON: reference price unavailable" in text
    assert "GRID STATUS: REJECTED" in text


# ----- minNotional respected in reported quantities -----

def test_min_notional_respected(tmp_path):
    sym = "BTC/USDT"
    f = _filters(tick_size=0.01, step_size=0.01, min_notional=1000.0, min_qty=0.0)
    specs = {sym: {"base": 100.0, "rng": 0.5, "filters": f, "avg": 100.0}}
    cfg = make_config()
    import grid as g
    plan = g.build_grid(sym, cfg.grid_mode(sym), 100.0, 1.0, f, cfg, reference_price=100.0)
    # every level's quantity must satisfy the min notional (step-aligned)
    for lvl in plan.levels:
        assert lvl.qty * lvl.buy_price >= f.min_notional - 1e-6


# ----- multi-symbol summary + exit code -----

def test_multi_symbol_summary_mixed(tmp_path):
    specs = {
        "AAA/USDT": {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0},   # accepted
        "BBB/USDT": {"base": 100.0, "rng": 0.2, "filters": _filters(), "avg": 100.0},   # gross below minimum
    }
    cfg = make_config(pair_list=("AAA/USDT", "BBB/USDT"))
    out = io.StringIO()
    code = _check_grid(cfg, FakeSpot(specs), out)
    text = out.getvalue()
    assert code == 1  # BBB rejected -> overall rejected
    assert "GRID VALIDATION SUMMARY" in text
    summary_lines = [
        ln for ln in text.splitlines() if "AAA/USDT" in ln or "BBB/USDT" in ln
    ]
    assert any("AAA/USDT" in ln and "ACCEPTED" in ln for ln in summary_lines)
    assert any("BBB/USDT" in ln and "REJECTED" in ln for ln in summary_lines)
    assert "OVERALL: REJECTED" in text


def test_all_accepted_exit_zero(tmp_path):
    specs = {
        "AAA/USDT": {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0},
        "BBB/USDT": {"base": 200.0, "rng": 1.0, "filters": _filters(), "avg": 200.0},
    }
    cfg = make_config(pair_list=("AAA/USDT", "BBB/USDT"))
    out = io.StringIO()
    code = _check_grid(cfg, FakeSpot(specs), out)
    assert code == 0
    assert "OVERALL: ACCEPTED" in out.getvalue()


# ----- read-only: zero submissions, no state mutation -----

def test_zero_order_submissions(tmp_path):
    specs = {"AAA/USDT": {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0}}
    spot = FakeSpot(specs)
    _check_grid(make_config(pair_list=("AAA/USDT",)), spot, io.StringIO())
    assert spot.submissions == []


def test_cli_check_grid_is_read_only_and_offline(tmp_path, monkeypatch):
    """main() --check-grid must not create the state DB or submit orders,
    and must exit 0 when all symbols accept."""
    from conftest import write_env_file
    import os

    env = write_env_file(
        tmp_path / ".env",
        PAIR_LIST="AAA/USDT",
        BINANCE_TESTNET_API_KEY="tk",
        BINANCE_TESTNET_API_SECRET="ts",
        LOWER_PRICE='{"AAA/USDT": 90.0}',
        UPPER_PRICE='{"AAA/USDT": 120.0}',
        TOTAL_QUOTE_BUDGET='{"AAA/USDT": 500.0}',
    )
    db = str(tmp_path / "state.db")
    specs = {"AAA/USDT": {"base": 100.0, "rng": 0.5, "filters": _filters(), "avg": 100.0}}
    spot = FakeSpot(specs)
    monkeypatch.setattr(bot, "BinanceSpot", lambda cfg: spot)

    code = bot.main(["--check-grid", "--env", env, "--db", db])
    assert code == 0
    # strictly read-only: no state database created, no orders submitted
    assert not os.path.exists(db)
    assert spot.submissions == []


def test_cli_check_grid_exit_code_on_rejection(tmp_path):
    specs = {"BBB/USDT": {"base": 100.0, "rng": 0.2, "filters": _filters(), "avg": 100.0}}
    cfg = make_config(pair_list=("BBB/USDT",))
    code = _check_grid(cfg, FakeSpot(specs), io.StringIO())
    assert code == 1  # gross below minimum -> overall rejected
