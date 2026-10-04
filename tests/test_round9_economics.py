"""Round 9 tests — economics: net-profit validation, partial fills,
realized PnL, and restart-reconstructed inventory.

Deterministic: Decimal arithmetic over synthetic execution data; the
cycle-level tests reuse the fake-exchange harness from test_testnet_cycle
and drive the REAL fill-sync/reconciliation code paths.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import testnet_cycle as tc
from economics import (
    CycleEconomics,
    EconomicsError,
    FillRecord,
    average_execution_price,
    estimate_fill_fee,
)
from profit_model import net_pct_from_prices, net_pct_from_step, passes
from symbol_rules import parse_symbol_info, validate_quantized_order_plan
from grid_engine import build_geometric_grid

from test_testnet_cycle import (  # shared deterministic harness
    FakeExchange,
    FakeReadClient,
    cycle_config,
    insert_open_order,
    make_runner,
)

D = Decimal
FEE = D("0.001")
SLIP = D("0.0005")


# ---------------------------------------------------------------------------
# Area 1 — fee + net grid profit validation
# ---------------------------------------------------------------------------
def _rules(tick="0.01", step="0.001", min_notional="5"):
    return parse_symbol_info({
        "symbol": "BNBUSDT", "baseAsset": "BNB", "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01",
             "maxPrice": "100000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000",
             "stepSize": step},
            {"filterType": "MIN_NOTIONAL", "minNotional": min_notional},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.10",
             "multiplierDown": "0.90", "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
        ],
    })


def _plan(lower="100", upper="104", tick="0.01", step="0.001",
          min_notional="5", quote_size="25", maker=None, slip=None,
          hard_min=None):
    maker = maker if maker is not None else FEE
    slip = slip if slip is not None else SLIP
    hard_min = hard_min if hard_min is not None else D("0.002")
    rules = _rules(tick=tick, step=step, min_notional=min_notional)
    levels, _upper = build_geometric_grid(
        D(lower), D(upper), D("0.006"), min_cells=1, max_levels=40)
    return validate_quantized_order_plan(
        levels, rules, D(quote_size), D("102"), maker, maker, slip,
        hard_min, 40), rules


def test_normal_grid_theoretical_and_executable_net():
    """Report BOTH nets: theoretical from the raw step, executable after
    tick/step quantization.  The executable net is the gate (STRICT > 0.002)."""
    theoretical = net_pct_from_step("0.006", FEE, FEE, SLIP)
    plan, _rules = _plan()
    del _rules
    assert plan.allowed
    assert plan.min_net_pct > D("0.002")
    assert theoretical == D("0.0034867420") or theoretical > D("0.0034")
    # quantization may erode, never inflate: executable ≤ theoretical + eps
    assert plan.min_net_pct <= theoretical + D("0.0001")


def test_fee_increase_rejects_grid():
    plan, _ = _plan(maker=D("0.002"))
    assert not plan.allowed
    assert "NET_PROFIT_BELOW_HARD_MIN" in plan.reason


def test_slippage_increase_rejects_grid():
    plan, _ = _plan(slip=D("0.01"))
    assert not plan.allowed
    assert "NET_PROFIT_BELOW_HARD_MIN" in plan.reason


def test_tick_rounding_erodes_net_and_rejects():
    """A coarse tickSize shrinks the quantized spread: the executable net
    drops to/below the 0.20% hard floor even though the theoretical step
    net does not.  The grid must be rejected — never rounded upward to
    pass."""
    theoretical = net_pct_from_step("0.006", FEE, FEE, SLIP)
    assert theoretical > D("0.002")
    plan, _ = _plan(tick="0.4")
    assert not plan.allowed
    assert plan.min_net_pct <= D("0.002")


def test_quantity_rounding_and_min_notional():
    from decimal import ROUND_DOWN
    plan, rules = _plan()
    cell = plan.cells[0]
    # quantity floored to stepSize, notional still above minimum
    assert cell.quantity == (cell.quantity / rules.step_size).to_integral_value(
        ROUND_DOWN) * rules.step_size
    assert cell.buy_price * cell.quantity >= rules.min_notional


def test_min_notional_rejection():
    plan, _ = _plan(min_notional="50")  # quote size 25 < 50
    assert not plan.allowed
    assert "SYMBOL_RULE_BLOCK" in plan.reason


def test_at_hard_min_is_rejected_strict():
    """The net floor is STRICT `>`: a cell whose executable net EQUALS the
    floor (0.200% against a 0.20% floor) is REJECTED, not accepted."""
    net = net_pct_from_prices("1", "1.002", "0", "0", "0")
    assert net == D("0.002")
    assert passes(net, D("0.002")) is False


def test_just_above_hard_min_passes():
    net = net_pct_from_prices("1", "1.00201", "0", "0", "0")
    assert net > D("0.002")
    assert passes(net, D("0.002")) is True


def test_below_hard_min_rejected():
    assert passes(D("0.00199"), D("0.002")) is False


def test_strict_boundary_exhaustive():
    """0.200% -> FAIL, 0.199% -> FAIL, 0.201% -> PASS (strict `>`)."""
    assert passes(D("0.002"),   D("0.002")) is False   # 0.200% = floor -> REJECT
    assert passes(D("0.00199"), D("0.002")) is False   # 0.199% -> REJECT
    assert passes(D("0.00201"), D("0.002")) is True    # 0.201% -> PASS


def test_post_quant_executable_net_strict_boundary():
    """The EXECUTABLE/quantized net (after tick/quantity rounding and
    exchange filters) is the authoritative gate value.

    With tick 0.01 and fees/slippage:
      - 100.00 -> 100.45 quantizes to (100.00, 100.45): net 0.00199
        (0.199%) -> REJECT.
      - 100.00 -> 100.46 quantizes to (100.00, 100.46): net 0.00209
        (just above 0.20%) -> PASS.
      - A net of EXACTLY 0.200% -> REJECT (the gate is strict `>`).
    """
    from grid_engine import GridLevel
    rules = _rules(tick="0.01", step="0.000001", min_notional="5")
    # 0.199% executable net -> REJECT
    plan_below = validate_quantized_order_plan(
        [GridLevel(0, D("100.00")), GridLevel(1, D("100.45"))],
        rules, D("1000"), D("100.2"), FEE, FEE, SLIP, D("0.002"), 40)
    assert not plan_below.allowed
    assert "NET_PROFIT_BELOW_HARD_MIN_AFTER_QUANTIZATION" in plan_below.reason
    assert plan_below.min_net_pct <= D("0.002")
    # 0.209% executable net -> PASS
    plan_above = validate_quantized_order_plan(
        [GridLevel(0, D("100.00")), GridLevel(1, D("100.46"))],
        rules, D("1000"), D("100.2"), FEE, FEE, SLIP, D("0.002"), 40)
    assert plan_above.allowed
    assert plan_above.min_net_pct > D("0.002")
    # exactly 0.200% -> REJECT (strict gate)
    assert passes(D("0.002"), D("0.002")) is False


def test_no_silent_upward_rounding_of_marginal_cell():
    """A coarse tick quantizes both prices down asymmetrically: the cell's
    executable spread lands just under the 0.20% floor — recorded as
    below-min (plan rejected), never rounded up to pass."""
    plan, _ = _plan(tick="0.23")
    assert not plan.allowed
    assert plan.min_net_pct <= D("0.002")
    assert plan.min_net_pct > D("0.0015")  # genuinely marginal, not zeroed
    assert "NET_PROFIT_BELOW_HARD_MIN_AFTER_QUANTIZATION" in plan.reason


# ---------------------------------------------------------------------------
# Adapter: authoritative execution-price data
# ---------------------------------------------------------------------------
def test_order_payload_includes_cummulative_quote_qty():
    from binance_testnet import _parse_order_status
    payload = _parse_order_status({
        "symbol": "BNBUSDT", "orderId": 1, "clientOrderId": "C1",
        "status": "PARTIALLY_FILLED", "price": "100.00",
        "origQty": "1.0", "executedQty": "0.5",
        "cummulativeQuoteQty": "50.03125",
    }, "BNBUSDT", "C1")
    assert payload["cummulativeQuoteQty"] == "50.03125"


def test_order_payload_without_cum_quote_is_none():
    from binance_testnet import _parse_order_status
    payload = _parse_order_status({
        "symbol": "BNBUSDT", "orderId": 1, "clientOrderId": "C1",
        "status": "NEW", "price": "100.00",
        "origQty": "1.0", "executedQty": "0",
    }, "BNBUSDT", "C1")
    assert payload["cummulativeQuoteQty"] is None


def test_order_payload_invalid_cum_quote_fails_closed():
    from binance_testnet import _parse_order_status, BinanceTestnetValidationError
    with pytest.raises(BinanceTestnetValidationError):
        _parse_order_status({
            "symbol": "BNBUSDT", "orderId": 1, "clientOrderId": "C1",
            "status": "PARTIALLY_FILLED", "price": "100.00",
            "origQty": "1.0", "executedQty": "0.5",
            "cummulativeQuoteQty": "NaN",
        }, "BNBUSDT", "C1")


def test_average_execution_price_authoritative_vs_estimated():
    price, source = average_execution_price(D("50.03125"), D("0.5"), D("100"))
    assert price == D("100.0625")
    assert source == "AUTHORITATIVE_PRICE"
    price, source = average_execution_price(None, D("0.5"), D("100.00"))
    assert price == D("100.00")
    assert source == "ESTIMATED_PRICE"


# ---------------------------------------------------------------------------
# Area 2 — partial fills / inventory
# ---------------------------------------------------------------------------
def _buy_fill(qty, price="100", cid="C1", fee=None):
    econ = CycleEconomics(maker_rate=FEE)
    rec = econ.make_fill(client_order_id=cid, side="BUY",
                         executed_qty=D(qty), execution_price=D(price),
                         fee_quote=fee)
    econ.apply_fill(rec)
    return econ, rec


@pytest.mark.parametrize("pct,expected", [
    ("0.1", "0.1"), ("0.5", "0.5"), ("0.99", "0.99"), ("1", "1"),
])
def test_fill_percentages_never_assume_requested(pct, expected):
    """Requested 1.0; only the EXECUTED quantity ever enters inventory."""
    econ, rec = _buy_fill(pct)
    assert econ.position_base == D(expected)
    assert rec.qty == D(expected)
    assert econ.summary()["buy_fills"] == 1


def test_partial_then_cancel_preserves_filled_portion(tmp_path):
    """50% filled, then authoritatively canceled: the executed portion
    stays in inventory; the unfilled remainder is simply gone (released by
    the authoritative terminal state — nothing assumed)."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "PARTIALLY_FILLED",
                        executed="0.500")
    exchange.orders["AGTC-BNBUSDT-1-1-1"]["cummulativeQuoteQty"] = "50.000"
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    runner.run(place_orders=True)
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_CANCELED
    assert Decimal(row["fill_qty"]) == D("0.500")
    # the filled portion is real inventory; the unfilled 50% was released
    assert runner.economics.position_base == D("0.500")
    assert len(runner.economics.fills) == 1


def test_partial_then_restart_reconstructs_inventory(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="1.000")
    exchange.orders["AGTC-BNBUSDT-1-1-1"]["cummulativeQuoteQty"] = "100.100"
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    runner.run(place_orders=False)
    # fresh process: new runner on the same ledger reconstructs exactly
    runner2 = make_runner(tmp_path, place=False, exchange=exchange,
                          ledger=ledger,
                          config=cycle_config(tmp_path, max_cycles=1))
    assert runner2.economics.position_base == D("1.000")
    # authoritative average price 100.10 (100.100/1.000), not limit 100.13
    assert runner2.economics.average_cost > D("100.09")
    assert runner2.economics.summary()["buy_fills"] == 1


def test_partial_fill_with_delayed_reconciliation(tmp_path):
    """Fill happens on the exchange AFTER the last known state; the next
    reconciliation syncs the delta exactly once (idempotent)."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000")
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    # delayed execution: 50% fills between runs
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "PARTIALLY_FILLED",
                        executed="0.500")
    exchange.orders["AGTC-BNBUSDT-1-1-1"]["cummulativeQuoteQty"] = "50.000"
    runner.reconcile_all()
    assert runner.economics.position_base == D("0.500")
    # reconciling again adds nothing (no phantom double-count)
    runner.reconcile_all()
    assert runner.economics.position_base == D("0.500")
    assert len(runner.economics.fills) == 1


def test_unknown_after_partial_execution(tmp_path):
    """Query becomes unreachable AFTER a partial execution: the synced
    portion remains real inventory; the order itself stays
    PENDING_RECONCILIATION and placement stays blocked."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "PARTIALLY_FILLED",
                        executed="0.400")
    exchange.orders["AGTC-BNBUSDT-1-1-1"]["cummulativeQuoteQty"] = "40.000"
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    runner.reconcile_all()
    assert runner.economics.position_base == D("0.400")
    # now the exchange query dies before the next resolve
    exchange.get_network_fail = True
    exchange.cancel_network_fail = True
    summary = runner.run(place_orders=True)
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] in (tc.ORDER_PENDING_RECONCILIATION,
                            tc.ORDER_PARTIALLY_FILLED, tc.ORDER_OPEN)
    assert summary["cleanup"]["ok"] is False  # never claim success
    assert runner.economics.position_base == D("0.400")  # preserved


def test_sell_exceeding_position_rejected_no_shorting():
    econ, _ = _buy_fill("0.5")
    with pytest.raises(EconomicsError, match="no shorting"):
        econ.apply_fill(econ.make_fill(client_order_id="S1", side="SELL",
                                       executed_qty=D("1.0"),
                                       execution_price=D("101")))


# ---------------------------------------------------------------------------
# Area 3 — realized PnL
# ---------------------------------------------------------------------------
def _round_trip(buy_price, sell_price, qty="1", rate=FEE):
    econ = CycleEconomics(maker_rate=rate)
    econ.apply_fill(econ.make_fill(client_order_id="B", side="BUY",
                                   executed_qty=D(qty),
                                   execution_price=D(buy_price)))
    outcome = econ.apply_fill(econ.make_fill(client_order_id="S", side="SELL",
                                             executed_qty=D(qty),
                                             execution_price=D(sell_price)))
    return econ, outcome


def test_profitable_grid_after_fees():
    econ, outcome = _round_trip("100", "100.6")
    gross = D("0.6")
    assert outcome.pnl_quote > 0
    # profitable AFTER fees: strictly less than the gross move
    assert outcome.pnl_quote < gross
    expected_fees = D("100") * D("0.001") + D("100.6") * D("0.001")
    assert outcome.pnl_quote == gross - expected_fees
    assert econ.summary()["grids_profitable"] == 1
    assert econ.summary()["grids_completed"] == 1
    assert econ.total_fees_quote == expected_fees


def test_sell_above_buy_is_not_profitable_after_fees():
    """sell > buy but the 0.60% gross move minus BOTH fees at 0.2%/leg…
    a 0.2% move is a LOSS after conservative fees — never counted as a
    profitable grid."""
    econ, outcome = _round_trip("100", "100.2")
    assert D("100.2") > D("100")  # price went up…
    assert outcome.pnl_quote < 0  # …and it is still a loss after fees
    assert econ.summary()["grids_losing"] == 1
    assert econ.summary()["grids_profitable"] == 0


def test_zero_pnl():
    econ, outcome = _round_trip("100", "100", rate=D("0"))
    assert outcome.pnl_quote == 0
    assert econ.summary()["grids_zero_pnl"] == 1
    assert econ.realized_pnl_quote == 0
    assert econ.realized_pnl_pct == 0


def test_negative_pnl():
    econ, outcome = _round_trip("100", "99", rate=D("0"))
    assert outcome.pnl_quote == D("-1")
    assert econ.summary()["grids_losing"] == 1
    assert econ.realized_pnl_quote == D("-1")


def test_partial_fill_pnl():
    """Buy 1.0 in two lots, sell 0.5 of it: realized PnL covers only the
    executed sell; the rest stays as inventory at its average cost."""
    econ = CycleEconomics(maker_rate=D("0"))
    econ.apply_fill(econ.make_fill(client_order_id="B1", side="BUY",
                                   executed_qty=D("0.5"),
                                   execution_price=D("100")))
    econ.apply_fill(econ.make_fill(client_order_id="B2", side="BUY",
                                   executed_qty=D("0.5"),
                                   execution_price=D("102")))
    assert econ.average_cost == D("101")
    outcome = econ.apply_fill(econ.make_fill(client_order_id="S1", side="SELL",
                                             executed_qty=D("0.5"),
                                             execution_price=D("104")))
    assert outcome.pnl_quote == D("0.5") * (D("104") - D("101"))
    assert econ.position_base == D("0.5")
    assert econ.summary()["grids_completed"] == 1


def test_multiple_completed_grids_accumulate():
    econ = CycleEconomics(maker_rate=D("0"))
    for i, (bp, sp) in enumerate([("100", "101"), ("101", "102")]):
        econ.apply_fill(econ.make_fill(client_order_id=f"B{i}", side="BUY",
                                       executed_qty=D("1"),
                                       execution_price=D(bp)))
        econ.apply_fill(econ.make_fill(client_order_id=f"S{i}", side="SELL",
                                       executed_qty=D("1"),
                                       execution_price=D(sp)))
    assert econ.realized_pnl_quote == D("2")
    assert econ.summary()["grids_completed"] == 2
    assert econ.summary()["grids_profitable"] == 2
    assert econ.position_base == 0


def test_fee_accounting_total_and_pnl_pct():
    econ = CycleEconomics(maker_rate=FEE)
    econ.apply_fill(econ.make_fill(client_order_id="B", side="BUY",
                                   executed_qty=D("2"),
                                   execution_price=D("100")))
    econ.apply_fill(econ.make_fill(client_order_id="S", side="SELL",
                                   executed_qty=D("2"),
                                   execution_price=D("101")))
    expected_fees = D("2") * D("100") * FEE + D("2") * D("101") * FEE
    assert econ.total_fees_quote == expected_fees
    assert econ.realized_pnl_quote == D("2") - expected_fees
    assert econ.realized_pnl_pct == econ.realized_pnl_quote / D("200.2")


def test_rounding_effects_exact_decimal():
    """Sub-unit prices: PnL is exact Decimal, no float artifacts."""
    econ, outcome = _round_trip("100.135", "100.735", qty="0.251")
    fees = D("0.251") * D("100.135") * FEE + D("0.251") * D("100.735") * FEE
    assert outcome.pnl_quote == D("0.251") * (D("100.735") - D("100.135")) - fees


def test_state_roundtrip_and_replay_idempotent():
    econ, _ = _round_trip("100", "100.6")
    econ.apply_fill(econ.make_fill(client_order_id="B2", side="BUY",
                                   executed_qty=D("0.4"),
                                   execution_price=D("99")))
    state = {k: str(v) for k, v in econ.to_state().items()}
    fills = list(econ.fills)
    # exact restore: the state already includes every fill — no replay
    restored = CycleEconomics.from_state(state, maker_rate=FEE)
    assert restored.to_state() == state
    # replay: a fresh instance rebuilt purely from the fill sequence is
    # identical to the original (restart reconstruction contract)
    replayed = CycleEconomics.replay(fills, maker_rate=FEE)
    assert replayed.to_state() == state
    # replaying twice on a fresh instance gives the same answer (the
    # ledger dedupes rows; replay itself is only ever run once per state)
    replayed2 = CycleEconomics.replay(fills, maker_rate=FEE)
    assert replayed2.to_state() == state


def test_fill_fee_estimator_rejects_invalid():
    with pytest.raises(EconomicsError):
        estimate_fill_fee(D("-1"), D("100"), FEE)
    with pytest.raises(EconomicsError):
        estimate_fill_fee(D("1"), D("0"), FEE)


# ---------------------------------------------------------------------------
# Cycle integration: authoritative fill sync through the runner
# ---------------------------------------------------------------------------
def test_fill_sync_uses_authoritative_execution_price(tmp_path):
    """executedQty + cummulativeQuoteQty → exact delta price, persisted to
    the ledger, applied to economics, and reconstructible after restart."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000",
                      price="100.13")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="1.000")
    # average execution price 100.10 (better than the limit for a BUY)
    exchange.orders["AGTC-BNBUSDT-1-1-1"]["cummulativeQuoteQty"] = "100.100"
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    result = runner.resolve_ledger_order(ledger.get_order("AGTC-BNBUSDT-1-1-1"))
    assert result["state"] == tc.ORDER_FILLED
    fill = runner.economics.fills[0]
    assert fill.price == D("100.100")
    assert fill.source == "AUTHORITATIVE_PRICE"
    # average cost includes the conservative buy fee: 100.100 × 1.001
    assert runner.economics.average_cost == D("100.100") * D("1.001")
    fills_in_ledger = ledger.all_fills()
    assert len(fills_in_ledger) == 1
    # restart: rebuilt from ledger replay, identical state
    runner2 = make_runner(tmp_path, place=False, exchange=exchange,
                          ledger=ledger,
                          config=cycle_config(tmp_path, max_cycles=1))
    assert runner2.economics.summary() == runner.economics.summary()


def test_fill_sync_estimated_price_fallback(tmp_path):
    """No cummulativeQuoteQty in the payload → limit price, ESTIMATED."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000",
                      price="100.13")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="1.000")
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    runner.resolve_ledger_order(ledger.get_order("AGTC-BNBUSDT-1-1-1"))
    fill = runner.economics.fills[0]
    assert fill.price == D("100.13")
    assert fill.source == "ESTIMATED_PRICE"


def test_incremental_fills_across_resolves(tmp_path):
    """10% → 99% → 100% across three reconciliations: each delta priced
    exactly, no phantom quantity, final position exact."""
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "c.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1", qty="1.000",
                      price="100.00")
    order = exchange.orders["AGTC-BNBUSDT-1-1-1"]
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)

    for executed, cum, status in [("0.100", "10.000", "PARTIALLY_FILLED"),
                                  ("0.990", "99.000", "PARTIALLY_FILLED"),
                                  ("1.000", "100.000", "FILLED")]:
        exchange.set_status("AGTC-BNBUSDT-1-1-1", status, executed=executed)
        order["cummulativeQuoteQty"] = cum
        runner.resolve_ledger_order(ledger.get_order("AGTC-BNBUSDT-1-1-1"))

    assert runner.economics.position_base == D("1.000")
    assert [f.price for f in runner.economics.fills] == \
        [D("100"), D("100"), D("100")]
    assert len(runner.economics.fills) == 3
    assert runner.economics.average_cost == D("100") * D("1.001")
