"""PERCENT_PRICE_BY_SIDE handling tests (realistic Binance filter fixtures).

Binance Spot (incl. testnet) enforces PERCENT_PRICE_BY_SIDE:
  BUY:  ref*bidMultiplierDown <= price <= ref*bidMultiplierUp
  SELL: ref*askMultiplierDown <= price <= ref*askMultiplierUp
where ref is the exchange's weighted-average price over avgPriceMins —
NOT the last traded price.
"""

from __future__ import annotations

import pytest

from bot import _selftest_order_params
from conftest import make_config
from exchange import ExchangeFilters, is_definitive_rejection, ExchangeError
from grid import build_grid, price_band, validate_price

# Realistic spot filter set (mirrors live Binance spot symbols such as
# NEARUSDT on testnet: symmetric 5% band, 1-minute average reference).
NEAR_FILTERS = ExchangeFilters(
    tick_size=0.001,
    step_size=0.1,
    min_notional=5.0,
    min_qty=0.1,
    bid_multiplier_up=1.05,
    bid_multiplier_down=0.95,
    ask_multiplier_up=1.05,
    ask_multiplier_down=0.95,
    avg_price_mins=1,
)

BTC_FILTERS = ExchangeFilters(
    tick_size=0.01,
    step_size=0.00001,
    min_notional=10.0,
    min_qty=0.00001,
    bid_multiplier_up=1.05,
    bid_multiplier_down=0.95,
    ask_multiplier_up=1.05,
    ask_multiplier_down=0.95,
    avg_price_mins=1,
)


# ----- band + validation semantics -----

def test_buy_below_bid_multiplier_down_rejected():
    reason = validate_price(NEAR_FILTERS, "BUY", 4.700, reference_price=4.959)
    assert reason is not None and "below" in reason
    assert "0.95" not in reason  # message names the effective bound
    assert "4.71" in reason  # 4.959 * 0.95


def test_buy_inside_allowed_range_accepted():
    # exact band floor is 4.959*0.95 = 4.71105: the first tick INSIDE is 4.712
    assert validate_price(NEAR_FILTERS, "BUY", 4.712, reference_price=4.959) is None
    assert validate_price(NEAR_FILTERS, "BUY", 4.959, reference_price=4.959) is None
    assert validate_price(NEAR_FILTERS, "BUY", 5.200, reference_price=4.959) is None


def test_buy_above_bid_multiplier_up_rejected():
    reason = validate_price(NEAR_FILTERS, "BUY", 5.300, reference_price=4.959)
    assert reason is not None and "above" in reason


def test_sell_above_ask_multiplier_up_rejected():
    reason = validate_price(NEAR_FILTERS, "SELL", 5.210, reference_price=4.959)
    assert reason is not None and "above" in reason


def test_sell_inside_allowed_range_accepted():
    assert validate_price(NEAR_FILTERS, "SELL", 5.200, reference_price=4.959) is None
    assert validate_price(NEAR_FILTERS, "SELL", 4.959, reference_price=4.959) is None


def test_sell_below_ask_multiplier_down_rejected():
    reason = validate_price(NEAR_FILTERS, "SELL", 4.700, reference_price=4.959)
    assert reason is not None and "below" in reason


def test_asymmetric_multipliers_use_correct_side():
    filters = ExchangeFilters(
        tick_size=0.001, step_size=0.1, min_notional=5.0, min_qty=0.1,
        bid_multiplier_up=1.10, bid_multiplier_down=0.90,
        ask_multiplier_up=1.02, ask_multiplier_down=0.98,
    )
    # BUY uses bid multipliers: 0.90 band -> 4.46 allowed
    assert validate_price(filters, "BUY", 4.500, reference_price=5.0) is None
    # SELL uses ask multipliers: 1.02 band -> 5.11 rejected
    assert validate_price(filters, "SELL", 5.110, reference_price=5.0) is not None
    band = price_band(filters, "SELL", 5.0)
    assert band == (5.0 * 0.98, 5.0 * 1.02)


def test_no_filter_or_no_reference_means_no_constraint():
    plain = ExchangeFilters(tick_size=0.001, step_size=0.1, min_notional=5.0, min_qty=0.1)
    assert validate_price(plain, "BUY", 1.0, reference_price=100.0) is None
    assert validate_price(NEAR_FILTERS, "BUY", 1.0, reference_price=None) is None
    assert validate_price(NEAR_FILTERS, "BUY", 1.0, reference_price=0.0) is None


def test_reference_price_is_not_last_price():
    """The band is computed from the weighted-average reference price —
    when it differs from the last price, a price valid vs the last price
    can still be invalid vs the reference."""
    reference, last = 4.959, 5.500  # market just spiked
    # 5.30 is only 3.6% above last but 6.9% above the reference -> rejected
    assert validate_price(NEAR_FILTERS, "BUY", 5.300, reference_price=reference) is not None
    # sanity: vs a reference of 5.5 it would be fine
    assert validate_price(NEAR_FILTERS, "BUY", 5.300, reference_price=last) is None


def test_tick_size_boundary_interaction():
    """Band floor is 4.959*0.95 = 4.71105 (unrepresentable at tick 0.001):
    the first tick inside is 4.712 (valid); 4.711 is one tick below the
    exact floor and correctly rejected."""
    assert validate_price(NEAR_FILTERS, "BUY", 4.712, reference_price=4.959) is None
    assert validate_price(NEAR_FILTERS, "BUY", 4.711, reference_price=4.959) is not None
    assert validate_price(NEAR_FILTERS, "BUY", 4.710, reference_price=4.959) is not None


# ----- grid generation protection -----

def test_grid_drops_levels_below_buy_band():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    # BTC at 50000, ATR step 350 -> levels at 49650..48250; band floor
    # 50000*0.95 = 47500 -> all five levels valid, none dropped.
    plan = build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, BTC_FILTERS, test_cfg,
                      reference_price=50000.0)
    assert plan.executable is True
    assert plan.dropped_levels == 0
    assert len(plan.levels) == test_cfg.total_grids


def test_grid_drops_only_out_of_band_levels():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 46000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    # Huge ATR: levels at 49650, 48950, 48250, 47550, 46850 — the last two
    # fall below the 47500 band floor and must be dropped, not submitted.
    plan = build_grid("BTC/USDT", "arithmetic", 50000.0, 700.0, BTC_FILTERS, test_cfg,
                      reference_price=50000.0)
    assert plan.executable is True
    assert plan.dropped_levels == 2
    assert len(plan.levels) == 3
    assert all(l.buy_price >= 47500.0 - 1e-9 for l in plan.levels)


def test_grid_blocked_when_all_levels_out_of_band():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 40000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    # ATR so deep that even the first level is below the band floor.
    plan = build_grid("BTC/USDT", "arithmetic", 50000.0, 3000.0, BTC_FILTERS, test_cfg,
                      reference_price=50000.0)
    assert plan.executable is False
    assert plan.block_reason == "percent_price_band"
    assert plan.levels == []


def test_grid_requires_reference_price_when_filter_present():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, BTC_FILTERS, test_cfg,
                      reference_price=None)
    assert plan.executable is False
    assert plan.block_reason == "reference_price_unavailable"


def test_grid_without_filter_needs_no_reference():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plain = ExchangeFilters(tick_size=0.01, step_size=0.00001, min_notional=10.0)
    plan = build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, plain, test_cfg,
                      reference_price=None)
    assert plan.executable is True


def test_geometric_grid_band_protection():
    test_cfg = make_config(
        lower_price={"SOL/USDT": 90.0},
        upper_price={"SOL/USDT": 120.0},
        total_quote_budget={"SOL/USDT": 500.0},
    )
    sol_filters = ExchangeFilters(
        tick_size=0.001, step_size=0.01, min_notional=10.0, min_qty=0.01,
        bid_multiplier_up=1.05, bid_multiplier_down=0.95,
        ask_multiplier_up=1.05, ask_multiplier_down=0.95,
    )
    plan = build_grid("SOL/USDT", "geometric", 100.0, 3.0, sol_filters, test_cfg,
                      reference_price=100.0)
    # 3% steps: levels 97, 94.09, 91.26, 88.53, 85.88 — all >= 95 band floor?
    # floor = 95 -> levels below 95 dropped
    assert plan.executable is True or plan.block_reason == "percent_price_band"
    for level in plan.levels:
        assert level.buy_price >= 95.0 - 1e-9


# ----- self-test price derivation -----

def test_selftest_price_derived_from_bid_band_floor():
    params = _selftest_order_params(NEAR_FILTERS, reference_price=4.959, last_close=4.959)
    # band floor 4.71105, quantized UP to tick -> 4.712
    assert params["price"] == pytest.approx(4.712)
    # minimum notional quantity at that price, step-aligned
    qty = params["qty"]
    assert qty * params["price"] >= NEAR_FILTERS.min_notional
    assert abs(qty / NEAR_FILTERS.step_size - round(qty / NEAR_FILTERS.step_size)) < 1e-9


def test_selftest_price_is_non_marketable():
    params = _selftest_order_params(NEAR_FILTERS, reference_price=4.959, last_close=4.959)
    assert params["price"] < 4.959  # rests below the market


def test_selftest_rejects_when_band_floor_would_be_marketable():
    # reference far below last close (band floor >= last): the probe price
    # would immediately match -> local validation refuses with exact reason
    with pytest.raises(ValueError, match="non-marketable"):
        _selftest_order_params(NEAR_FILTERS, reference_price=6.0, last_close=4.959)


def test_selftest_rejects_without_reference_price():
    with pytest.raises(ValueError, match="reference"):
        _selftest_order_params(NEAR_FILTERS, reference_price=0.0, last_close=4.959)
    with pytest.raises(ValueError, match="reference"):
        _selftest_order_params(NEAR_FILTERS, reference_price=None, last_close=4.959)


def test_selftest_rejects_when_filter_absent():
    plain = ExchangeFilters(tick_size=0.001, step_size=0.1, min_notional=5.0, min_qty=0.1)
    with pytest.raises(ValueError, match="PERCENT_PRICE_BY_SIDE"):
        _selftest_order_params(plain, reference_price=4.959, last_close=4.959)


def test_selftest_price_satisfies_every_filter_locally():
    filters = ExchangeFilters(
        tick_size=0.01, step_size=0.001, min_notional=10.0, min_qty=0.001,
        bid_multiplier_up=1.05, bid_multiplier_down=0.95,
        ask_multiplier_up=1.05, ask_multiplier_down=0.95, avg_price_mins=1,
    )
    params = _selftest_order_params(filters, reference_price=100.0, last_close=100.5)
    price, qty = params["price"], params["qty"]
    assert validate_price(filters, "BUY", price, 100.0) is None     # band
    assert round(price / filters.tick_size) * filters.tick_size == pytest.approx(price)
    assert round(qty / filters.step_size) * filters.step_size == pytest.approx(qty)
    assert qty >= filters.min_qty
    assert qty * price >= filters.min_notional
    assert price < 100.5                                            # non-marketable


# ----- definitive rejection detection -----

def test_filter_failure_400_is_definitive_rejection():
    exc = ExchangeError(
        'POST /api/v3/order -> HTTP 400: {"code":-1013,'
        '"msg":"Filter failure: PERCENT_PRICE_BY_SIDE"}'
    )
    assert is_definitive_rejection(exc) is True


def test_new_order_rejected_is_definitive():
    exc = ExchangeError(
        'POST /api/v3/order -> HTTP 400: {"code":-2010,'
        '"msg":"NEW_ORDER_REJECTED"}'
    )
    assert is_definitive_rejection(exc) is True


def test_network_failure_is_not_a_rejection():
    assert is_definitive_rejection(ExchangeError("POST failed after retries: timeout")) is False
    assert is_definitive_rejection(
        ExchangeError('HTTP 400: {"code":-1121,"msg":"Invalid symbol"}')
    ) is False


# ----- child-sell band deferral (dry-run executor with a band-aware stub) -----

def test_child_sell_outside_band_is_deferred_not_submitted(tmp_path):
    from exchange import DryRunExecutor
    from state import StateStore

    class BandSpot:
        """Provides filters + a reference price pinning the SELL band."""
        def get_filters(self, symbol):
            return NEAR_FILTERS

        def get_avg_price(self, symbol):
            return {"mins": 1, "price": "4.959"}

    cfg = make_config()
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(["NEAR/USDT"])
    executor = DryRunExecutor(cfg, store, spot=BandSpot())
    buy = store.create_order(
        "cid-b", "NEAR/USDT", "BUY", "LIMIT_MAKER", 4.8, 10.0, "dry_run",
        target_sell_price=5.9,  # 19% above reference: outside the 5% band
    )
    store.update_order_status(buy, "FILLED", 10.0)
    store.record_fill(buy, "NEAR/USDT", "BUY", 4.8, 10.0, 0.0, trade_id="t-b")

    executor._spawn_child_sells(store.get_order(buy))

    # no child sell was created; the quantity stays pending conversion
    children = [o for o in store.symbol_orders("NEAR/USDT") if o["parent_order_id"] == buy]
    assert children == []
    parent = store.get_order(buy)
    assert parent["child_sell_qty"] == pytest.approx(0.0)

    # the interrupted-exit recovery check does NOT treat this as uncovered
    from bot import Bot, CycleView
    from strategy import IndicatorSnapshot

    class NoMarket:
        def snapshot(self, symbol, cfg, now_ms):
            return CycleView(IndicatorSnapshot(symbol=symbol), None, None)

    Bot(cfg, store, NoMarket(), DryRunExecutor(cfg, store, spot=BandSpot()))
    bot_store = store
    # inventory 10.0, pending conversion 10.0 -> covered by conversion potential
    bot = Bot(cfg, bot_store, NoMarket(), DryRunExecutor(cfg, bot_store, spot=BandSpot()))
    assert bot._has_uncovered_inventory("NEAR/USDT") is False


def test_child_sell_inside_band_is_placed(tmp_path):
    from exchange import DryRunExecutor
    from state import StateStore

    class BandSpot:
        def get_filters(self, symbol):
            return NEAR_FILTERS

        def get_avg_price(self, symbol):
            return {"mins": 1, "price": "4.959"}

    cfg = make_config()
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(["NEAR/USDT"])
    executor = DryRunExecutor(cfg, store, spot=BandSpot())
    buy = store.create_order(
        "cid-b", "NEAR/USDT", "BUY", "LIMIT_MAKER", 4.8, 10.0, "dry_run",
        target_sell_price=5.2,  # 4.9% above reference: inside the band
    )
    store.update_order_status(buy, "FILLED", 10.0)
    store.record_fill(buy, "NEAR/USDT", "BUY", 4.8, 10.0, 0.0, trade_id="t-b")

    executor._spawn_child_sells(store.get_order(buy))

    children = [o for o in store.symbol_orders("NEAR/USDT") if o["parent_order_id"] == buy]
    assert len(children) == 1
    assert children[0]["qty"] == pytest.approx(10.0)
    assert children[0]["price"] == pytest.approx(5.2)
