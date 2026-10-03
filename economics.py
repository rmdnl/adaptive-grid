"""Round 9 — deterministic inventory + realized-PnL accounting for the
testnet cycle path.

This module serves the CYCLE harness only; the paper engine keeps its own
complete accounting (`paper_accounting.py`) and the two never mix.

Core rules:

* **Authoritative data only.**  Fills enter through executed quantities and
  execution prices taken from exchange payloads — never from the requested
  quantity or intended price when authoritative data exists.
* **No phantom inventory, no lost inventory.**  BUY fills increase the base
  position by exactly the executed amount; SELL fills decrease it by
  exactly the executed amount; a SELL exceeding the held position raises
  (Spot only — shorting is impossible at this layer, not merely forbidden).
* **Realized PnL is AFTER fees.**  A grid completion is profitable only
  when sell proceeds minus buy cost minus BOTH fees is strictly positive —
  `sell_price > buy_price` alone proves nothing.
* **Average-cost basis.**  BUY fills recompute the average cost including
  their fee; SELL releases basis at the current average cost.
* **Deterministic.**  Decimal arithmetic, fixed precision, replay-safe:
  applying the same fill sequence always produces identical state, and
  `to_state`/`from_state` round-trips exactly.

Fee model: the repository-wide conservative fallback maker rate applied to
the executed notional (`qty × price × rate`).  This is deliberately
conservative and deterministic; per-trade commission data
(GET /api/v3/myTrades) is a future refinement and must only ever REPLACE
the estimate when authoritative, never be skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Any, Optional

getcontext().prec = 40

_D = Decimal


class EconomicsError(RuntimeError):
    """Accounting-layer invariant violation (fail closed)."""


#: Fill price provenance.
PRICE_SOURCE_AUTHORITATIVE = "AUTHORITATIVE_PRICE"
PRICE_SOURCE_ESTIMATED = "ESTIMATED_PRICE"


@dataclass(frozen=True)
class FillRecord:
    """One executed fill applied to the position.

    `qty` and `price` come from the exchange (executedQty and the average
    execution price).  `fee_quote` is the conservative fee for this fill in
    quote currency.  `source` records whether the execution price was
    authoritative (cummulativeQuoteQty / executedQty) or estimated from the
    limit price.
    """

    client_order_id: str
    side: str  # BUY / SELL
    price: Decimal
    qty: Decimal
    fee_quote: Decimal
    source: str

    @property
    def key(self) -> tuple:
        """Replay-safe identity of a fill."""
        return (self.client_order_id, self.side, str(self.price),
                str(self.qty), self.source)


def estimate_fill_fee(executed_qty: Decimal, executed_price: Decimal,
                      maker_rate: Decimal) -> Decimal:
    """Conservative fee estimate for one fill (quote currency)."""
    qty = _D(executed_qty)
    price = _D(executed_price)
    rate = _D(maker_rate)
    if qty < 0 or price <= 0 or rate < 0 or rate >= 1:
        raise EconomicsError(
            f"invalid fee inputs: qty={qty} price={price} rate={rate}")
    return qty * price * rate


def average_execution_price(cumulative_quote_qty: Optional[Decimal],
                            executed_qty: Decimal,
                            limit_price: Decimal) -> tuple[Decimal, str]:
    """Authoritative average execution price when provable, else estimate.

    Returns ``(price, source)``.  The average price is authoritative only
    when BOTH `cummulativeQuoteQty` and `executedQty` are present, finite
    and positive, and their quotient is finite and positive.  Otherwise the
    limit price is used and the record is explicitly ESTIMATED — never
    silently presented as execution truth.
    """
    qty = _D(executed_qty)
    limit = _D(limit_price)
    if qty < 0 or limit <= 0:
        raise EconomicsError(
            f"invalid execution inputs: qty={qty} limit={limit}")
    if cumulative_quote_qty is not None and qty > 0:
        cum = _D(cumulative_quote_qty)
        if cum.is_finite() and cum > 0:
            avg = cum / qty
            if avg.is_finite() and avg > 0:
                return avg, PRICE_SOURCE_AUTHORITATIVE
    return limit, PRICE_SOURCE_ESTIMATED


@dataclass(frozen=True)
class GridCloseOutcome:
    """Result of one SELL that closes basis (a completed grid leg)."""

    pnl_quote: Decimal
    pnl_pct: Decimal
    cost_basis: Decimal
    proceeds: Decimal


class CycleEconomics:
    """Average-cost position + realized-PnL tracker over authoritative fills."""

    def __init__(self, *, maker_rate: Decimal) -> None:
        rate = _D(maker_rate)
        if rate < 0 or rate >= 1:
            raise EconomicsError(f"maker_rate must be in [0, 1): {rate}")
        self._maker_rate = rate
        self._position_base = _D("0")
        self._average_cost = _D("0")
        self._total_cost_invested = _D("0")  # cumulative buy basis (for pct)
        self._realized_pnl = _D("0")
        self._total_fees = _D("0")
        self._grids_completed = 0
        self._grids_profitable = 0
        self._grids_losing = 0
        self._grids_zero = 0
        self._buy_fills = 0
        self._sell_fills = 0
        self._fills: list[FillRecord] = []

    # -- reads ----------------------------------------------------------------
    @property
    def position_base(self) -> Decimal:
        return self._position_base

    @property
    def average_cost(self) -> Decimal:
        return self._average_cost

    @property
    def realized_pnl_quote(self) -> Decimal:
        return self._realized_pnl

    @property
    def realized_pnl_pct(self) -> Decimal:
        """Realized PnL relative to the cumulative invested buy basis."""
        if self._total_cost_invested <= 0:
            return _D("0")
        return self._realized_pnl / self._total_cost_invested

    @property
    def total_fees_quote(self) -> Decimal:
        return self._total_fees

    @property
    def fills(self) -> tuple[FillRecord, ...]:
        return tuple(self._fills)

    def summary(self) -> dict[str, Any]:
        return {
            "position_base": str(self._position_base),
            "average_cost": str(self._average_cost),
            "realized_pnl_quote": str(self._realized_pnl),
            "realized_pnl_pct": str(self.realized_pnl_pct),
            "total_fees_quote": str(self._total_fees),
            "grids_completed": self._grids_completed,
            "grids_profitable": self._grids_profitable,
            "grids_losing": self._grids_losing,
            "grids_zero_pnl": self._grids_zero,
            "buy_fills": self._buy_fills,
            "sell_fills": self._sell_fills,
        }

    def to_state(self) -> dict[str, str]:
        return {
            "position_base": str(self._position_base),
            "average_cost": str(self._average_cost),
            "total_cost_invested": str(self._total_cost_invested),
            "realized_pnl": str(self._realized_pnl),
            "total_fees": str(self._total_fees),
            "grids_completed": str(self._grids_completed),
            "grids_profitable": str(self._grids_profitable),
            "grids_losing": str(self._grids_losing),
            "grids_zero": str(self._grids_zero),
            "buy_fills": str(self._buy_fills),
            "sell_fills": str(self._sell_fills),
        }

    @classmethod
    def from_state(cls, state: dict[str, str], *,
                   maker_rate: Decimal) -> "CycleEconomics":
        """Restore EXACT persisted state (the counters already include every
        applied fill — do not replay fills on top of this)."""
        instance = cls(maker_rate=maker_rate)
        instance._position_base = _D(state["position_base"])
        instance._average_cost = _D(state["average_cost"])
        instance._total_cost_invested = _D(state["total_cost_invested"])
        instance._realized_pnl = _D(state["realized_pnl"])
        instance._total_fees = _D(state["total_fees"])
        instance._grids_completed = int(state["grids_completed"])
        instance._grids_profitable = int(state["grids_profitable"])
        instance._grids_losing = int(state["grids_losing"])
        instance._grids_zero = int(state["grids_zero"])
        instance._buy_fills = int(state["buy_fills"])
        instance._sell_fills = int(state["sell_fills"])
        return instance

    @classmethod
    def replay(cls, fills: list[FillRecord], *,
               maker_rate: Decimal) -> "CycleEconomics":
        """Rebuild state from scratch by applying fills in order.

        The ledger's append-only fill rows are the source of truth for
        restart reconstruction; a fresh instance plus deterministic replay
        reproduces the exact pre-restart state.
        """
        instance = cls(maker_rate=maker_rate)
        for record in fills:
            instance.apply_fill(record)
        return instance

    # -- mutation ----------------------------------------------------------------
    def apply_fill(self, record: FillRecord) -> dict[str, Any]:
        """Apply one authoritative fill.  Deterministic and replay-safe."""
        if record.qty <= 0 or record.price <= 0:
            raise EconomicsError(
                f"fill qty/price must be positive: {record.qty}/{record.price}")
        if record.side == "BUY":
            return self._apply_buy(record)
        if record.side == "SELL":
            return self._apply_sell(record)
        raise EconomicsError(f"unknown fill side: {record.side!r}")

    def make_fill(self, *, client_order_id: str, side: str,
                  executed_qty: Decimal, execution_price: Decimal,
                  fee_quote: Optional[Decimal] = None) -> FillRecord:
        """Build a fill with the conservative fee estimate applied."""
        fee = (fee_quote if fee_quote is not None else
               estimate_fill_fee(executed_qty, execution_price,
                                 self._maker_rate))
        return FillRecord(
            client_order_id=client_order_id, side=side,
            price=_D(execution_price), qty=_D(executed_qty),
            fee_quote=_D(fee), source=PRICE_SOURCE_AUTHORITATIVE)

    def _apply_buy(self, record: FillRecord) -> dict[str, Any]:
        cost = record.qty * record.price
        basis = cost + record.fee_quote
        old_base = self._position_base
        new_base = old_base + record.qty
        if new_base <= 0:
            raise EconomicsError("buy fill produced non-positive position")
        # average cost includes the buy fee (a real acquisition cost)
        new_avg = (self._average_cost * old_base + basis) / new_base
        self._position_base = new_base
        self._average_cost = new_avg
        self._total_cost_invested += basis
        self._total_fees += record.fee_quote
        self._buy_fills += 1
        self._fills.append(record)
        return {"side": "BUY", "position_base": str(new_base),
                "average_cost": str(new_avg)}

    def _apply_sell(self, record: FillRecord) -> GridCloseOutcome:
        if record.qty > self._position_base:
            # SELL exceeding held inventory would be shorting — impossible
            # on Binance Spot and rejected here (fail closed, no phantom).
            raise EconomicsError(
                f"SELL {record.qty} exceeds held position "
                f"{self._position_base} (no shorting)")
        proceeds = record.qty * record.price - record.fee_quote
        cost_basis = record.qty * self._average_cost
        pnl = proceeds - cost_basis
        self._position_base -= record.qty
        self._realized_pnl += pnl
        self._total_fees += record.fee_quote
        self._sell_fills += 1
        self._grids_completed += 1
        if pnl > 0:
            self._grids_profitable += 1
        elif pnl < 0:
            self._grids_losing += 1
        else:
            self._grids_zero += 1
        self._fills.append(record)
        return GridCloseOutcome(
            pnl_quote=pnl,
            pnl_pct=(pnl / cost_basis) if cost_basis > 0 else _D("0"),
            cost_basis=cost_basis,
            proceeds=record.qty * record.price,
        )
