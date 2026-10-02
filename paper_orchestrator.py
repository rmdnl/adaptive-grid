"""Phase 5D — Deterministic Paper-Trading Orchestration Layer.

Coordinates the already-certified component pipeline:

  Market Data → Market Intelligence → Adaptive Grid Planner →
  Grid Lifecycle → Inventory Allocation → Order Intents →
  Paper Order Engine → Paper Fill Engine → Paper Accounting →
  Recovery/Reconciliation → Risk/Equity → next cycle.

Hard invariants:
- DRY_RUN mandatory: no live Binance trading, ever.
- NO modification of main.py / grid_planner.py / grid_lifecycle.py /
  inventory_model.py / order_engine.py / paper_accounting.py.
- Deterministic: no wall-clock time as logical input.
- Idempotent: replay with same cycle_id produces same result.
- Immutable dataclasses for all cycle inputs and results.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from grid_planner import (
    ActivePlan,
    AdaptiveGridPlan,
    PlanDecision,
    evaluate_adaptive_grid_plan,
)
from market_regime import MarketRegime
from grid_lifecycle import (
    LifecycleManager,
    LifecycleError,
    LifecycleState as GridLifecycleState,
    ValidationErrorCode,
)
from order_engine import (
    OrderIntent,
    OrderState,
    PaperOrder,
    PaperOrderEngine,
    PaperStateUnhealthyError,
    make_client_order_id,
)
from paper_accounting import (
    PaperAccountingEngine,
    PaperAccountState,
)
from inventory_model import (
    allocate_grid,
    can_execute_allocation,
    filter_actionable_cells,
    InventorySnapshot,
    LifecycleState,
)
from recovery import (
    RecoveryUnhealthyError,
    recover_paper_state,
)
from risk_engine import RiskDecision
from storage import cycle_transaction
from symbol_rules import SymbolRules, quantize_price
from profit_model import net_pct_from_prices

logger = logging.getLogger("paper_orchestrator")

# ---------------------------------------------------------------------------
# Immutable cycle data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperCycleInput:
    """Immutable input to a deterministic orchestration cycle.

    No wall-clock time anywhere. All temporal information is encoded
    through candle_index (monotonically increasing integer) and
    timestamps pre-fetched from market data.
    """
    candle_index: int
    symbol: str
    # Market data (pre-fetched, no network calls inside orchestrator)
    current_price: Decimal
    kline_df: Any  # pandas DataFrame of closed 15m candles
    quote: Any | None = None  # MarketQuote or None
    # Range
    lower_price: Decimal = Decimal("0")
    upper_price: Decimal = Decimal("0")
    # Active plan (None if no plan active)
    active_plan: ActivePlan | None = None
    # Market intelligence (pre-computed by caller; orchestrator derives
    # fallbacks from the active plan when omitted)
    regime: Any = None  # MarketRegime or None
    range_quality_score: Decimal = Decimal("0")
    # Config
    cfg: dict = field(default_factory=dict)
    # Fees
    maker_fee: Decimal = Decimal("0.001")
    taker_fee: Decimal = Decimal("0.001")
    fee_asset: str = "USDT"
    # Risk
    risk_decision: RiskDecision = field(default_factory=lambda: RiskDecision(True))
    # Market freshness
    max_candle_age_seconds: int = 5400
    max_quote_age_seconds: int = 10
    # Clock function (for internal ordering only, never for logical decisions)
    clock: Any = None
    # Client order prefix
    client_order_prefix: str = "AG"
    # Dry run flag (must be True)
    dry_run: bool = True
    # Metadata
    metadata: dict = field(default_factory=dict)
    # Symbol rules (optional; used for allocation quantization)
    rules: SymbolRules | None = None


@dataclass(frozen=True)
class PaperCycleEvent:
    """Immutable event record within a cycle."""
    event_id: str
    cycle_id: str
    event_type: str
    payload: dict
    created_at: str


@dataclass(frozen=True)
class PaperCycleResult:
    """Immutable result of a deterministic orchestration cycle."""
    cycle_id: str
    candle_index: int
    symbol: str
    # Plan outcome
    plan_decision: PlanDecision | None = None
    plan: AdaptiveGridPlan | None = None
    lifecycle_transition: str | None = None
    # Orders
    order_intents: tuple = ()
    orders_submitted: int = 0
    orders_skipped: int = 0
    # Fills
    fills_applied: int = 0
    fills_idempotent: int = 0
    fills_skipped: int = 0
    # State
    accounting_state: dict | None = None
    recovery_healthy: bool = True
    # Events
    events: tuple = ()
    # Error tracking
    error: str | None = None
    blocked_reason: str | None = None
    # Metadata
    is_idempotent: bool = False
    success: bool = True


# ---------------------------------------------------------------------------
# Allocation result (carries intents + block diagnostics)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _AllocationResult:
    """Result of PaperOrchestrator._generate_order_intents.

    Carries both the generated intents and, when the allocation was
    blocked/failed (as opposed to merely producing zero actionable cells),
    deterministic diagnostics so the caller can emit an ALLOCATION_BLOCKED
    cycle event.
    """
    intents: list[OrderIntent]
    # When allocation was blocked (not just zero actionable cells),
    # this carries {"status","reasons","plan_id","generation"}.
    # None when allocation succeeded (even if zero actionable cells).
    allocation_blocked: dict | None = None
    # FIX 4A: deterministic per-cell reasons for cells dropped by the
    # post-quantization profit gate (empty tuple when nothing was dropped).
    # This is a cell-level filter, NOT a whole-allocation block: valid cells
    # still produce intents, and the caller records these reasons so a
    # fully-gated cycle still emits a deterministic PROFIT_GATE_BLOCKED event.
    profit_blocked_cells: tuple[str, ...] = ()


def _post_quant_net_profit(
    raw_buy_price: Decimal,
    raw_sell_price: Decimal,
    rules: SymbolRules,
    buy_fee: Decimal,
    sell_fee: Decimal,
    slippage: Decimal,
) -> Decimal | None:
    """Authoritative post-quantization round-trip net profit for one grid cell.

    FIX 4A. Reuses the SAME helpers that main.py's
    ``validate_quantized_order_plan`` uses, so both execution paths share one
    profit formula (no duplication):

      - ``quantize_price``  (symbol_rules)  → the exact tick-quantized
        execution price, fail-closed on rule violation.
      - ``net_pct_from_prices`` (profit_model) → round-trip net profit
        including buy/sell fees and roundtrip slippage.

    The pre-quantization / configured-step profit is NOT used. Returns the
    actual post-quantization net pct, or ``None`` when quantization or profit
    computation fails (caller fails closed → blocks the cell).
    """
    try:
        buy = quantize_price(raw_buy_price, rules)
        sell = quantize_price(raw_sell_price, rules)
        if buy <= 0 or sell <= 0:
            return None
        if sell <= buy:
            # Quantization removed the positive spread → no executable profit.
            return None
        return net_pct_from_prices(buy, sell, buy_fee, sell_fee, slippage)
    except (ArithmeticError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cycle identity: deterministic hash
# ---------------------------------------------------------------------------

def _risk_decision_identity(risk_decision: RiskDecision) -> str:
    """Deterministic identity string for a RiskDecision.

    RiskDecision is a frozen dataclass (allowed, reasons).  The tuple of
    reason strings is its stable deterministic representation -- no
    timestamps, no randomness.  Returns the canonical JSON string used
    for hashing.
    """
    return json.dumps(
        {"allowed": risk_decision.allowed, "reasons": list(risk_decision.reasons)},
        sort_keys=True,
    )


def generate_cycle_id(
    candle_index: int,
    symbol: str,
    plan_id: str,
    risk_decision: RiskDecision | None = None,
) -> str:
    """Deterministic cycle identity: same logical inputs -> same cycle_id.

    No randomness, no wall-clock time, no mutable state.  The cycle_id is
    a SHA-256 hash of the canonical representation of all *logical* inputs
    that define the cycle.  ``open_order_ids`` (mutable state created by a
    previous cycle's own side effects) is intentionally excluded so that a
    restart at the same candle produces the same cycle_id and hits the
    idempotency cache instead of re-executing.

    Parameters
    ----------
    risk_decision:
        Optional.  When provided, its ``(allowed, reasons)`` tuple is
        folded into the hash so that logically different risk contexts
        for the same candle produce distinct cycle IDs.
    """
    risk_identity = _risk_decision_identity(risk_decision) if risk_decision is not None else ""
    payload = json.dumps({
        "candle_index": candle_index,
        "symbol": symbol,
        "plan_id": plan_id or "",
        "risk": risk_identity,
    }, sort_keys=True)
    return "cycle_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Market freshness validation
# ---------------------------------------------------------------------------

def validate_market_freshness(cycle_input: PaperCycleInput) -> str | None:
    """Validate that market data is fresh enough for a safe cycle.

    Returns None if fresh, error message if stale/invalid.
    Uses pre-fetched timestamps; never calls the network.
    """
    clock = cycle_input.clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Validate kline DataFrame has data and close_time is fresh
    df = cycle_input.kline_df
    if df is None or not hasattr(df, 'iloc') or len(df) == 0:
        return "No kline data available"

    last_close_time = df["close_time"].iloc[-1]
    if hasattr(last_close_time, "tzinfo"):
        if last_close_time.tzinfo is None:
            last_close_time = last_close_time.replace(tzinfo=timezone.utc)
    else:
        last_close_time = datetime.fromisoformat(str(last_close_time))
        if last_close_time.tzinfo is None:
            last_close_time = last_close_time.replace(tzinfo=timezone.utc)

    candle_age = (now - last_close_time).total_seconds()
    if candle_age < 0:
        return f"Kline close_time is in the future: {candle_age}s"
    if candle_age > cycle_input.max_candle_age_seconds:
        return f"Kline data is stale: age {candle_age:.1f}s > max {cycle_input.max_candle_age_seconds}s"

    # Validate quote freshness if provided
    if cycle_input.quote is not None:
        quote = cycle_input.quote
        if hasattr(quote, 'fetched_at') and quote.fetched_at is not None:
            fetched_at = quote.fetched_at
            if hasattr(fetched_at, "tzinfo"):
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            else:
                fetched_at = datetime.fromisoformat(str(fetched_at))
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            quote_age = (now - fetched_at).total_seconds()
            if quote_age < 0:
                return f"Quote fetched_at is in the future: {quote_age}s"
            if quote_age > cycle_input.max_quote_age_seconds:
                return f"Quote data is stale: age {quote_age:.1f}s > max {cycle_input.max_quote_age_seconds}s"

    return None


# ---------------------------------------------------------------------------
# PaperOrchestrator: deterministic cycle execution
# ---------------------------------------------------------------------------

class PaperOrchestrator:
    """Deterministic paper-trading orchestrator.

    Coordinates the component pipeline without modifying any existing modules.
    Each cycle is fully deterministic: same inputs → same outputs.

    The orchestrator does NOT:
    - Fetch market data (caller provides it)
    - Place real orders
    - Use wall-clock time as logical input
    - Modify main.py, grid_planner.py, or any existing module
    """

    def __init__(
        self,
        lifecycle_db_path: str,
        order_db_path: str,
        accounting_engine: PaperAccountingEngine,
        order_engine: PaperOrderEngine,
        client_order_prefix: str = "AG",
    ):
        self.lifecycle_manager = LifecycleManager(lifecycle_db_path)
        self.order_engine = order_engine
        self.accounting_engine = accounting_engine
        self.client_order_prefix = client_order_prefix
        # Patch 2D: the orchestrator owns the single cycle-level transaction
        # that spans both logical databases.  ``order_db_path`` is opened as
        # ``main``; when ``lifecycle_db_path`` is a different file it is
        # attached under the LIFECYCLE_SCHEMA alias so one BEGIN/COMMIT/
        # ROLLBACK atomically covers cycle-owned mutations in both.
        self.order_db_path = order_db_path
        self.lifecycle_db_path = lifecycle_db_path
        self._ensure_schema(order_db_path)

    def _ensure_schema(self, db_path: str) -> None:
        """Create orchestration persistence tables (idempotent)."""
        from storage import connect
        con = connect(db_path)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS paper_orch_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    candle_index INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    plan_id TEXT,
                    plan_decision TEXT,
                    lifecycle_transition TEXT,
                    orders_submitted INTEGER DEFAULT 0,
                    orders_skipped INTEGER DEFAULT 0,
                    fills_applied INTEGER DEFAULT 0,
                    fills_idempotent INTEGER DEFAULT 0,
                    fills_skipped INTEGER DEFAULT 0,
                    accounting_state_hash TEXT,
                    recovery_healthy INTEGER DEFAULT 1,
                    is_idempotent INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 1,
                    blocked_reason TEXT,
                    error TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS paper_orch_events (
                    event_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS paper_orch_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _record_cycle(
        self, db_path: str, cycle_id: str, candle_index: int,
        symbol: str, plan_id: str | None, plan_decision: PlanDecision | None,
        lifecycle_transition: str | None, orders_submitted: int,
        orders_skipped: int, fills_applied: int, fills_idempotent: int,
        fills_skipped: int, recovery_healthy: bool, is_idempotent: bool,
        success: bool, blocked_reason: str | None, error: str | None,
        metadata: dict, created_at: str, con=None,
    ) -> None:
        """Persist the cycle record.

        When ``con`` is supplied (cycle-transaction join) the record is
        written on the caller's connection and committed exactly once by the
        cycle owner.  When ``con`` is None it owns its own transaction
        (standalone, e.g. the pre-transaction market-blocked path).
        """
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
            con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """INSERT OR IGNORE INTO paper_orch_cycles
                (cycle_id, candle_index, symbol, plan_id, plan_decision,
                 lifecycle_transition, orders_submitted, orders_skipped,
                 fills_applied, fills_idempotent, fills_skipped,
                 recovery_healthy, is_idempotent, success, blocked_reason,
                 error, metadata, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, candle_index, symbol, plan_id,
                    plan_decision.value if plan_decision else None,
                    lifecycle_transition, orders_submitted, orders_skipped,
                    fills_applied, fills_idempotent, fills_skipped,
                    1 if recovery_healthy else 0,
                    1 if is_idempotent else 0,
                    1 if success else 0,
                    blocked_reason, error,
                    json.dumps(metadata) if metadata else None,
                    created_at,
                ),
            )
            if owns:
                con.commit()
        except Exception:
            if owns:
                con.rollback()
            raise
        finally:
            if owns:
                con.close()

    def _record_event(
        self, db_path: str, cycle_id: str, event_type: str,
        payload: dict, created_at: str, con=None,
    ) -> PaperCycleEvent:
        """Record one cycle event.

        When ``con`` is supplied the event INSERT joins the caller's
        transaction (committed/rolled back with the cycle); when ``con`` is
        None the method owns a standalone transaction.
        """
        event_id = hashlib.sha256(
            f"{cycle_id}:{event_type}:{created_at}:{json.dumps(payload, sort_keys=True, default=str)}".encode()
        ).hexdigest()[:20]
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
            con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """INSERT OR IGNORE INTO paper_orch_events
                (event_id, cycle_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (event_id, cycle_id, event_type,
                 json.dumps(payload, default=str), created_at),
            )
            if owns:
                con.commit()
        except Exception:
            if owns:
                con.rollback()
            raise
        finally:
            if owns:
                con.close()
        return PaperCycleEvent(
            event_id=event_id, cycle_id=cycle_id,
            event_type=event_type, payload=payload,
            created_at=created_at,
        )

    def _check_existing_cycle(
        self, db_path: str, cycle_id: str, con=None,
    ) -> dict | None:
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
        try:
            row = con.execute(
                "SELECT * FROM paper_orch_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            if owns:
                con.close()

    # ------------------------------------------------------------------
    # Lifecycle integrity gate (Patch 2C)
    # ------------------------------------------------------------------
    #
    # Authoritative source of truth is the lifecycle manager (database-backed),
    # NOT cached cycle-input state.  This gate is the hard safety check that a
    # valid, consistent lifecycle is in place BEFORE any order intent is
    # generated or submitted.  It fails closed on ANY inconsistency or
    # exception.  It does NOT create a second lifecycle state machine; it only
    # READS the lifecycle manager and cross-checks its own invariants.
    # ------------------------------------------------------------------
    # PHASE 7A -- Anomaly A1 fix: complete an in-flight reconfiguration
    # atomically within the cycle transaction so the lifecycle cannot wedge
    # in RECONFIGURATION_PENDING / READY_TO_RECONFIGURE.
    #
    # Before this fix the production path had NO caller for
    # validate_pending_reconfiguration()/finalize_reconfiguration(), so once
    # the planner emitted RECONFIGURATION_REQUIRED the state stayed PENDING
    # forever and every subsequent KEEP cycle hit INVALID_TRANSITION.  The
    # drain below invokes the EXISTING lifecycle completion API at the
    # correct point (top of Step 5, before handle_planner_decision), joined
    # to the same cycle transaction.  A completion failure is a LifecycleError
    # => the caller's hard-veto path => zero submissions + full rollback.
    # ------------------------------------------------------------------

    def _active_plan_state_to_planner(
        self, active, con, lc_prefix: str,
    ) -> ActivePlan | None:
        """Refresh a lifecycle ActivePlanState into a planner ActivePlan.

        Reads the authoritative lifecycle generation (F-4) so the planner and
        the integrity gate see the post-drain truth, not a stale pre-txn value.
        """
        if active is None:
            return None
        return ActivePlan(
            plan_id=active.plan_id,
            candidate_lower=active.candidate_lower,
            candidate_upper=active.candidate_upper,
            grid_step=active.grid_step,
            grid_count=active.grid_count,
            regime=MarketRegime(active.regime),
            range_quality_score=active.range_quality_score,
            candle_index=active.candle_index,
            generation=self.lifecycle_manager.get_generation(
                con=con, prefix=lc_prefix,
            ),
        )

    def _drain_pending_reconfiguration(
        self,
        cycle_input: PaperCycleInput,
        con,
        lc_prefix: str,
    ) -> None:
        """Complete any in-flight reconfiguration chain in-place.

        PENDING -> READY -> ACTIVE via the existing, idempotent lifecycle API,
        all joined to the caller's cycle transaction (``con``).  No-op when
        the state is not PENDING/READY.  Raises ``LifecycleError`` on a
        stale/corrupt/unresolvable candidate so the caller fails closed.
        """
        lm = self.lifecycle_manager
        state = lm.get_current_state(con=con, prefix=lc_prefix)
        if state not in (
            GridLifecycleState.RECONFIGURATION_PENDING,
            GridLifecycleState.READY_TO_RECONFIGURE,
        ):
            return

        active = lm.get_active_plan(con=con, prefix=lc_prefix)
        if active is None:
            # PENDING/READY with no active plan row is corrupt state.
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                "Cannot complete reconfiguration: no active plan found",
            )
        active_plan_id = active.plan_id

        if state is GridLifecycleState.RECONFIGURATION_PENDING:
            # PENDING -> READY (validates the candidate; raises if stale/
            # corrupt so an invalid candidate can never advance).
            lm.validate_pending_reconfiguration(
                active_plan_id,
                candle_index=cycle_input.candle_index,
                con=con,
                prefix=lc_prefix,
            )
        # READY -> ACTIVE (final swap: new generation, candidate becomes the
        # authoritative active plan; joins the same transaction).
        lm.finalize_reconfiguration(
            active_plan_id,
            candle_index=cycle_input.candle_index,
            con=con,
            prefix=lc_prefix,
        )

    def _rederive_planner_after_drain(
        self,
        cycle_input: PaperCycleInput,
        active_plan_state,
        db_path: str,
        con,
        lc_prefix: str,
    ) -> AdaptiveGridPlan:
        """Re-run the planner against the freshly-activated plan (PHASE 7A).

        Mirrors the pre-transaction derivation in ``run_cycle`` but keys off the
        refreshed lifecycle active plan (authoritative generation, F-4) so the
        post-drain decision is consistent with the plan that is now ACTIVE.
        """
        regime = (
            cycle_input.regime
            if cycle_input.regime is not None
            else MarketRegime(active_plan_state.regime)
        )
        range_quality_score = (
            cycle_input.range_quality_score
            if cycle_input.range_quality_score > 0
            else active_plan_state.range_quality_score
        )
        base_available = self._compute_planner_base_inventory(
            db_path, cycle_input,
        )
        plan = evaluate_adaptive_grid_plan(
            pair=cycle_input.symbol,
            regime=regime,
            range_quality_score=range_quality_score,
            current_price=cycle_input.current_price,
            configured_lower=cycle_input.lower_price,
            configured_upper=cycle_input.upper_price,
            available_base_inventory=base_available,
            cfg=cycle_input.cfg,
            active_plan=self._active_plan_state_to_planner(
                active_plan_state, con, lc_prefix,
            ),
            current_candle_index=cycle_input.candle_index,
        )
        return plan

    def _validate_lifecycle_integrity(
        self,
        cycle_input: PaperCycleInput,
        plan: AdaptiveGridPlan | None,
        plan_decision: PlanDecision | None,
        con=None,
        lc_prefix: str = "",
    ) -> str | None:
        """Return a reason string if the lifecycle is invalid, else ``None``.

        Enforces, before order generation:
          - lifecycle state is a recognised enum value (not corrupt)
          - when the lifecycle state requires an active plan, one exists
          - active plan generation is consistent with the manager generation
          - active plan is not from a stale generation
          - generation is a valid non-negative integer
          - pending reconfiguration (if any) is authoritative and not corrupt

        When ``con``/``lc_prefix`` are supplied (cycle-transaction join) every
        lifecycle read goes through the caller's connection so the gate sees
        the cycle's in-transaction lifecycle mutations rather than the stale
        committed state.
        """
        lm = self.lifecycle_manager
        state = lm.get_current_state(con=con, prefix=lc_prefix)

        # 1. Lifecycle state must be a recognised enum value.
        if not isinstance(state, GridLifecycleState):
            return f"INVALID_LIFECYCLE_STATE:{state!r}"

        # 2. Generation must be a valid non-negative integer.
        try:
            current_generation = lm.get_generation(con=con, prefix=lc_prefix)
        except Exception as exc:
            return f"CORRUPT_GENERATION:{exc}"
        if not isinstance(current_generation, int) or current_generation < 0:
            return f"INVALID_GENERATION:{current_generation!r}"

        active = lm.get_active_plan(con=con, prefix=lc_prefix)
        if active is not None:
            # Active plan generation must be a valid non-negative integer.
            if not isinstance(active.generation, int) or active.generation < 0:
                return f"INVALID_ACTIVE_GENERATION:{active.generation!r}"

        # 3. States that require an active plan: ACTIVE and READY_TO_RECONFIGURE
        #    must have one present and consistent.  RECONFIGURATION_PENDING keeps
        #    the prior active plan authoritative (trading continues on it).
        if state in (GridLifecycleState.ACTIVE, GridLifecycleState.READY_TO_RECONFIGURE):
            if active is None:
                return f"MISSING_ACTIVE_PLAN:{state.value}"

        # 4. Generation consistency:
        #    - ACTIVE: the active plan generation MUST equal the manager
        #      generation.  A gap means a finalized generation was lost
        #      (corrupt state) → stale generation → fail closed.
        #    - PENDING/READY: the manager generation is legitimately one ahead
        #      of the active plan (the pending candidate reserves it).  A gap
        #      beyond that is corrupt.
        if state is GridLifecycleState.ACTIVE and active is not None:
            if active.generation != current_generation:
                return (
                    f"GENERATION_MISMATCH:active={active.generation},"
                    f"manager={current_generation}"
                )
        elif state in (
            GridLifecycleState.RECONFIGURATION_PENDING,
            GridLifecycleState.READY_TO_RECONFIGURE,
        ):
            if active is not None:
                if current_generation not in (active.generation, active.generation + 1):
                    return (
                        f"GENERATION_MISMATCH:active={active.generation},"
                        f"manager={current_generation}"
                    )

        # 6. Pending reconfiguration integrity (only when the state holds one).
        #    The lifecycle manager is the authoritative validator; we mirror
        #    its own rules (see validate_pending_reconfiguration) so a corrupt
        #    or stale pending candidate can never become active.
        if state in (
            GridLifecycleState.RECONFIGURATION_PENDING,
            GridLifecycleState.READY_TO_RECONFIGURE,
        ):
            if active is None:
                return f"PENDING_WITHOUT_ACTIVE_PLAN:{state.value}"
            pending = lm.get_pending_reconfiguration(
                active.plan_id, con=con, prefix=lc_prefix,
            )
            # The state claims a reconfiguration is in flight; the candidate
            # must actually exist (corrupt state → fail closed).
            if pending is None:
                return f"PENDING_CANDIDATE_MISSING:{state.value}"
            # Stale candidate (manager's authoritative rule: candidate generation
            # must be strictly greater than the active plan's).
            if pending.generation <= active.generation:
                return (
                    f"STALE_PENDING_CANDIDATE:candidate={pending.generation},"
                    f"active={active.generation}"
                )
            # Candidate must reference the active plan it will replace.
            if pending.active_plan_id != active.plan_id:
                return (
                    f"PENDING_PLAN_MISMATCH:candidate_active="
                    f"{pending.active_plan_id},active={active.plan_id}"
                )
            # Pair/symbol must be consistent between active and candidate.
            if active.pair and pending.pair and active.pair != pending.pair:
                return (
                    f"PENDING_SYMBOL_MISMATCH:active={active.pair},"
                    f"candidate={pending.pair}"
                )

        # 7. If the planner produced an allowed plan and an active plan is
        #    present, the active plan's generation namespace must equal the
        #    manager generation so the generation-bound client_order_id used
        #    for the new intents stays collision-free across generations.
        if (
            plan_decision == PlanDecision.GRID_ALLOWED
            and plan is not None
            and active is not None
            and active.generation != current_generation
        ):
            return (
                f"PLAN_GENERATION_MISMATCH:active={active.generation},"
                f"manager={current_generation}"
            )

        return None

    # ------------------------------------------------------------------
    # Core deterministic cycle execution
    # ------------------------------------------------------------------

    def run_cycle(self, cycle_input: PaperCycleInput) -> PaperCycleResult:
        """Execute a single deterministic paper-trading cycle.

        This is the main entry point. Each call represents one closed candle
        being processed through the full component pipeline.

        15-step cycle order:
        1. Validate input
        2. Validate market freshness
        3. Market intelligence (classify regime, calculate features)
        4. Adaptive planner (generate candidate plan)
        5. Lifecycle manager (feed plan decision, get transition)
        6. Inventory allocation (read accounting state)
        7. Order intent generation (create intents from plan levels)
        8. Order submission (submit intents through PaperOrderEngine)
        9. Fill processing (deterministic price comparison for open orders)
        10. Accounting (handled by PaperOrderEngine internally)
        11. Recovery (validate state consistency)
        12. Re-read state (immutable snapshot)
        13. Record cycle + events
        14. Assemble immutable result
        15. Return result
        """
        db_path = self.order_engine.db_path
        clock = cycle_input.clock or (lambda: datetime.now(timezone.utc))
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_iso = now.isoformat()

        # Step 1: Validate input
        if cycle_input.candle_index < 0:
            return PaperCycleResult(
                cycle_id="", candle_index=cycle_input.candle_index,
                symbol=cycle_input.symbol, error="candle_index must be >= 0",
                success=False,
            )
        if not cycle_input.symbol:
            return PaperCycleResult(
                cycle_id="", candle_index=cycle_input.candle_index,
                symbol="", error="symbol must be non-empty", success=False,
            )
        # DRY_RUN is mandatory: the orchestrator refuses to execute any cycle
        # unless the caller explicitly opts into dry-run mode.
        if not cycle_input.dry_run:
            return PaperCycleResult(
                cycle_id="", candle_index=cycle_input.candle_index,
                symbol=cycle_input.symbol,
                error="DRY_RUN is mandatory; live trading is not supported",
                success=False,
            )

        # Step 2: Validate market freshness
        freshness_error = validate_market_freshness(cycle_input)
        if freshness_error is not None:
            events = []
            cycle_id = generate_cycle_id(
                cycle_input.candle_index, cycle_input.symbol, "",
                risk_decision=cycle_input.risk_decision,
            )
            # Idempotent replay: a previously-blocked cycle must replay exactly.
            existing = self._check_existing_cycle(db_path, cycle_id)
            if existing is not None:
                events = self._get_cycle_events(db_path, cycle_id)
                return PaperCycleResult(
                    cycle_id=cycle_id,
                    candle_index=cycle_input.candle_index,
                    symbol=cycle_input.symbol,
                    events=tuple(events),
                    blocked_reason=freshness_error,
                    is_idempotent=True,
                    success=False,
                )
            now_str = now_iso
            events.append(
                self._record_event(
                    db_path, cycle_id, "CYCLE_STARTED",
                    {"candle_index": cycle_input.candle_index,
                     "symbol": cycle_input.symbol}, now_str,
                )
            )
            events.append(
                self._record_event(
                    db_path, cycle_id, "MARKET_BLOCKED",
                    {"reason": freshness_error}, now_str,
                )
            )
            self._record_cycle(
                db_path, cycle_id, cycle_input.candle_index,
                cycle_input.symbol, None, None, None, 0, 0, 0, 0, 0,
                False, False, False, freshness_error, None, {}, now_str,
            )
            return PaperCycleResult(
                cycle_id=cycle_id, candle_index=cycle_input.candle_index,
                symbol=cycle_input.symbol, events=tuple(events),
                blocked_reason=freshness_error, success=False,
            )

        # Step 3-4: Market Intelligence + Adaptive Planner
        # Regime and range quality come from the caller's market intelligence
        # when provided; otherwise they fall back to the active plan, and
        # finally to conservative defaults (RANGE / 80).
        # Pre-transaction: planner decisions are computed against the
        # COMMITTED state, so these reads stay on standalone connections.
        plan_decision = None
        plan = None
        active_plan_state = self.lifecycle_manager.get_active_plan()

        regime = cycle_input.regime or (
            cycle_input.active_plan.regime
            if cycle_input.active_plan is not None
            else MarketRegime.RANGE
        )
        range_quality_score = (
            cycle_input.range_quality_score
            if cycle_input.range_quality_score > 0
            else (
                cycle_input.active_plan.range_quality_score
                if cycle_input.active_plan is not None
                else Decimal("80")
            )
        )
        base_available = self._compute_planner_base_inventory(
            db_path, cycle_input,
        )
        try:
            plan = evaluate_adaptive_grid_plan(
                pair=cycle_input.symbol,
                regime=regime,
                range_quality_score=range_quality_score,
                current_price=cycle_input.current_price,
                configured_lower=cycle_input.lower_price,
                configured_upper=cycle_input.upper_price,
                available_base_inventory=base_available,
                cfg=cycle_input.cfg,
                active_plan=cycle_input.active_plan,
                current_candle_index=cycle_input.candle_index,
            )
            plan_decision = plan.decision
        except Exception as exc:
            plan_decision = PlanDecision.GRID_BLOCKED
            logger.error("Adaptive planner error: %s", exc)

        # Generate cycle ID deterministically (before any side effects).
        # open_order_ids is mutable state — excluded from cycle identity.

        plan_id = plan.plan_id if plan else (
            cycle_input.active_plan.plan_id if cycle_input.active_plan else ""
        )
        cycle_id = generate_cycle_id(
            cycle_input.candle_index, cycle_input.symbol,
            plan_id,
            risk_decision=cycle_input.risk_decision,
        )

        # Check idempotency: if a cycle record for this cycle_id was already
        # committed (Patch 2A/2B semantics unchanged), return the cached result
        # instead of re-executing.  A rolled-back cycle (injected order/fill/
        # accounting failure inside the transaction) persists NO record, so a
        # retry of a failed cycle is not treated as idempotent and re-runs
        # cleanly (Test 6).  Committed clean-blocked cycles DO persist a
        # record, so they replay idempotently.
        existing = self._check_existing_cycle(db_path, cycle_id)
        if existing is not None:
            events = self._get_cycle_events(db_path, cycle_id)
            return PaperCycleResult(
                cycle_id=cycle_id,
                candle_index=cycle_input.candle_index,
                symbol=cycle_input.symbol,
                plan_decision=PlanDecision(existing["plan_decision"])
                if existing["plan_decision"] else None,
                lifecycle_transition=existing["lifecycle_transition"],
                orders_submitted=existing["orders_submitted"],
                orders_skipped=existing["orders_skipped"],
                fills_applied=existing["fills_applied"],
                fills_idempotent=existing["fills_idempotent"],
                fills_skipped=existing["fills_skipped"],
                recovery_healthy=bool(existing["recovery_healthy"]),
                is_idempotent=True,
                success=bool(existing["success"]),
                blocked_reason=existing["blocked_reason"],
                error=existing["error"],
                events=tuple(events),
            )

        # Patch 2D: own a SINGLE cycle-level transaction that atomically
        # covers every cycle-owned mutation -- lifecycle transition, order
        # submissions, reservation/accounting updates, paper fills, and the
        # cycle result record.  All lower-level components participate on
        # this connection and never commit independently; the transaction
        # commits exactly once on success and rolls back as a whole on any
        # failure, so no partial cycle mutation can ever remain.
        try:
            with cycle_transaction(
                self.order_db_path, self.lifecycle_db_path
            ) as (con, lc_prefix):
                return self._run_cycle_txn(
                    cycle_input, con, lc_prefix,
                    plan=plan,
                    plan_decision=plan_decision,
                    active_plan_state=active_plan_state,
                    cycle_id=cycle_id,
                    plan_id=plan_id,
                    now_iso=now_iso,
                )
        except Exception as exc:
            # Patch 2D (requirement H): the whole cycle transaction rolled
            # back, so NO order, fill, reservation, accounting, lifecycle, or
            # cycle-record mutation from this attempt survived.  Return a
            # deterministic failure result — do NOT persist a "successful"
            # cycle record for a rolled-back cycle.  A retry of the same
            # logical cycle then executes cleanly against the pre-cycle state
            # (Test 6).  The failure is surfaced via ``error`` + success=False.
            logger.error(
                "Cycle %s rolled back; no cycle-owned mutation persisted: %s",
                cycle_id, exc,
            )
            return PaperCycleResult(
                cycle_id=cycle_id,
                candle_index=cycle_input.candle_index,
                symbol=cycle_input.symbol,
                plan_decision=plan_decision,
                plan=plan,
                error=f"CYCLE_ROLLED_BACK:{exc}"[:400],
                success=False,
            )

    # ------------------------------------------------------------------
    # Cycle transaction body (Patch 2D)
    #
    # Runs inside the single cycle-level transaction owned by run_cycle.
    # Every mutable cycle effect (lifecycle mutation, order submissions,
    # accounting/reservation updates, fills, cycle record + events) is
    # issued on ``con`` -- the shared connection -- and never commits on
    # its own.  The outer ``cycle_transaction`` commits or rolls back
    # once, which is what makes the whole cycle atomic.
    # ------------------------------------------------------------------

    def _run_cycle_txn(
        self,
        cycle_input: PaperCycleInput,
        con,
        lc_prefix: str,
        *,
        plan,
        plan_decision,
        active_plan_state,
        cycle_id: str,
        plan_id: str,
        now_iso: str,
    ) -> PaperCycleResult:
        db_path = self.order_engine.db_path
        clock = cycle_input.clock or (lambda: datetime.now(timezone.utc))

        events: list[PaperCycleEvent] = []

        # CYCLE_STARTED (joins the cycle transaction)
        events.append(
            self._record_event(
                db_path, cycle_id, "CYCLE_STARTED",
                {"candle_index": cycle_input.candle_index,
                 "symbol": cycle_input.symbol,
                 "plan_id": plan_id or None}, now_iso,
                con=con,
            )
        )

        # Step 5: Lifecycle manager -- mutation joins the cycle transaction,
        # so a lifecycle change can never outlive a later cycle failure.
        lifecycle_transition = None
        # Hard-veto flag: any LifecycleError makes this cycle fail closed.
        # It is part of the submission gate, not merely a recorded event.
        lifecycle_hard_veto = False

        # PHASE 7A (Anomaly A1): complete any in-flight reconfiguration
        # (PENDING -> READY -> ACTIVE) atomically inside this cycle
        # transaction, BEFORE evaluating the planner decision.  Without this,
        # a PENDING state wedges: a KEEP/RECONFIG decision from PENDING is
        # INVALID_TRANSITION and no production caller ever completed the
        # chain.  On success the freshly-activated plan becomes the
        # authoritative active plan and the planner decision is re-derived
        # against it; on failure the hard-veto path fails closed (zero
        # submit) and the deterministic blocked cycle commits, matching the
        # 2C hard-veto pattern.
        prev_lifecycle_state = self.lifecycle_manager.get_current_state(
            con=con, prefix=lc_prefix,
        )
        if prev_lifecycle_state in (
            GridLifecycleState.RECONFIGURATION_PENDING,
            GridLifecycleState.READY_TO_RECONFIGURE,
        ):
            try:
                self._drain_pending_reconfiguration(cycle_input, con, lc_prefix)
                # Refresh the authoritative active plan + generation, then
                # re-derive the planner decision against the newly-activated
                # plan (the pre-transaction plan/decision is now stale).
                active_plan_state = self.lifecycle_manager.get_active_plan(
                    con=con, prefix=lc_prefix,
                )
                plan = self._rederive_planner_after_drain(
                    cycle_input, active_plan_state, db_path, con, lc_prefix,
                )
                plan_decision = plan.decision
                lifecycle_transition = GridLifecycleState.ACTIVE.value
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECONFIGURATION_FINALIZED",
                        {"plan_id": plan.plan_id}, now_iso,
                        con=con,
                    )
                )
            except LifecycleError as exc:
                # Fail closed: a candidate that cannot be completed (stale,
                # corrupt, no active plan) must not advance.  The deterministic
                # blocked cycle commits and the wedge persists until the state
                # is reconciled -- no partial reconfiguration survives.
                lifecycle_hard_veto = True
                lifecycle_transition = f"ERROR:{exc.code.value}"
                events.append(
                    self._record_event(
                        db_path, cycle_id, "LIFECYCLE_ERROR",
                        {"error": str(exc), "code": exc.code.value,
                         "stage": "reconfig_drain"}, now_iso,
                        con=con,
                    )
                )

        if plan is not None and not lifecycle_hard_veto:
            try:
                if active_plan_state is not None:
                    transition = self.lifecycle_manager.handle_planner_decision(
                        plan, active_plan_state, cycle_input.cfg,
                        candle_index=cycle_input.candle_index,
                        con=con, prefix=lc_prefix,
                    )
                else:
                    transition = self.lifecycle_manager.handle_planner_decision(
                        plan, None, cycle_input.cfg,
                        candle_index=cycle_input.candle_index,
                        con=con, prefix=lc_prefix,
                    )
                lifecycle_transition = transition.to_state.value
            except LifecycleError as exc:
                # HARD VETO: a lifecycle failure (corrupt state, stale or
                # duplicate generation, rejected transition) makes this cycle
                # fail closed.  must_submit is forced False below; zero order
                # intents are generated and PaperOrderEngine.submit is never
                # called for this cycle.  The raised error unwinds the whole
                # cycle transaction, so any lifecycle mutation made earlier in
                # the same cycle is rolled back together with it (Test 4).
                lifecycle_hard_veto = True
                lifecycle_transition = f"ERROR:{exc.code.value}"
                events.append(
                    self._record_event(
                        db_path, cycle_id, "LIFECYCLE_ERROR",
                        {"error": str(exc), "code": exc.code.value}, now_iso,
                        con=con,
                    )
                )

            # Emit decision-specific events
            if plan_decision == PlanDecision.GRID_ALLOWED:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "PLAN_ACTIVATED",
                        {"plan_id": plan.plan_id,
                         "grid_count": plan.grid_count}, now_iso,
                        con=con,
                    )
                )
            elif plan_decision == PlanDecision.RECONFIGURATION_REQUIRED:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECONFIGURATION_PENDING",
                        {"plan_id": plan.plan_id}, now_iso,
                        con=con,
                    )
                )
                if lifecycle_transition == GridLifecycleState.READY_TO_RECONFIGURE.value:
                    events.append(
                        self._record_event(
                            db_path, cycle_id, "RECONFIGURATION_READY",
                            {"plan_id": plan.plan_id}, now_iso,
                            con=con,
                        )
                    )
            elif plan_decision == PlanDecision.GRID_BLOCKED:
                reasons = [r.value for r in plan.reasons] if plan and plan.reasons else []
                events.append(
                    self._record_event(
                        db_path, cycle_id, "GRID_BLOCKED",
                        {"plan_id": plan.plan_id if plan is not None else None,
                         "reasons": reasons}, now_iso,
                        con=con,
                    )
                )
            elif plan_decision == PlanDecision.KEEP_CURRENT_PLAN:
                pass  # No event needed; plan stays authoritative

        # Step 6: Inventory allocation -- read accounting state through the
        # cycle connection so the planner sees post-mutation cycle state.
        accounting_state = self._read_accounting_state(db_path, con=con)

        # Step 7-10: Order intents, submission, fill processing
        order_intents: list[OrderIntent] = []
        orders_submitted = 0
        orders_skipped = 0
        fills_applied = 0
        fills_idempotent = 0
        fills_skipped = 0

        # Step 7-pre: lifecycle integrity gate (authoritative source of truth
        # is the lifecycle manager, not cached cycle input).  Any exception
        # from the gate itself is a corrupt lifecycle state → fail closed.
        gate_blocked: dict | None = None
        gate_error: str | None = None
        if lifecycle_hard_veto:
            gate_blocked = {
                "code": "LIFECYCLE_ERROR",
                "reasons": [lifecycle_transition],
            }
        else:
            try:
                gate_error = self._validate_lifecycle_integrity(
                    cycle_input, plan, plan_decision,
                    con=con, lc_prefix=lc_prefix,
                )
                if gate_error is not None:
                    gate_blocked = {"code": "LIFECYCLE_INVALID", "reasons": [gate_error]}
            except Exception as exc:
                gate_error = f"GATE_EXCEPTION:{exc}"
                gate_blocked = {"code": "LIFECYCLE_CORRUPT", "reasons": [gate_error]}

        should_submit = (
            cycle_input.risk_decision.allowed
            and plan_decision is not None
            and plan is not None
            and plan_decision in (PlanDecision.GRID_ALLOWED,)
            and not lifecycle_hard_veto
            and gate_blocked is None
        )

        if gate_blocked is not None:
            events.append(
                self._record_event(
                    db_path, cycle_id, "LIFECYCLE_BLOCKED",
                    {
                        "code": gate_blocked["code"],
                        "reasons": list(gate_blocked.get("reasons", [])),
                    },
                    now_iso,
                    con=con,
                )
            )

        # Allocation block reason (propagated to cycle blocked_reason / success).
        allocation_blocked: dict | None = None
        # Open-order capacity block reason.
        capacity_blocked: dict | None = None

        if should_submit:
            alloc_result = self._generate_order_intents(
                cycle_input, plan, clock, accounting_state,
                con=con, lc_prefix=lc_prefix,
            )
            order_intents = alloc_result.intents
            allocation_blocked = alloc_result.allocation_blocked

            # FIX 4A: deterministic record of cells rejected by the
            # post-quantization profit gate.  When ALL actionable cells were
            # gated (zero intents) this is carried inside allocation_blocked
            # (PROFIT_GATE_BLOCKED) and emitted by the ALLOCATION_BLOCKED
            # event below; when only SOME were gated, record it here so the
            # rejection is never silent.
            if alloc_result.profit_blocked_cells and allocation_blocked is None:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "PROFIT_GATE_CELLS_BLOCKED",
                        {
                            "cells": list(alloc_result.profit_blocked_cells),
                            "plan_id": plan.plan_id,
                        },
                        now_iso,
                        con=con,
                    )
                )

            # Allocation failure/block → explicit ALLOCATION_BLOCKED event.
            # Zero actionable cells on a VALID allocation is NOT a block.
            if allocation_blocked is not None:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "ALLOCATION_BLOCKED",
                        dict(allocation_blocked), now_iso,
                        con=con,
                    )
                )
                # Skip submission; allocation produced no actionable cells.
                # (order_intents is already [] when allocation_blocked is set.)

            elif order_intents:
                # Open-order capacity gate (Fix 2 + PATCH 5B): existing open
                # orders + proposed new orders must not exceed the resolved
                # capacity limit.  PATCH 5B: an UNRESOLVABLE limit is never
                # treated as unlimited — it fails closed with a deterministic
                # reason so executable capacity is never inferred from
                # absence.
                capacity_limit = self._compute_open_order_capacity_limit(
                    cycle_input,
                )
                existing_open = len(self._get_open_order_ids(db_path, con=con))
                proposed = len(order_intents)
                if capacity_limit is None:
                    # PATCH 5B: no resolvable capacity limit → BLOCK.
                    capacity_blocked = {
                        "status": "OPEN_ORDER_CAPACITY_UNRESOLVED",
                        "existing_open_orders": existing_open,
                        "proposed_new_orders": proposed,
                        "max_open_orders": None,
                        "total": existing_open + proposed,
                    }
                    events.append(
                        self._record_event(
                            db_path, cycle_id,
                            "OPEN_ORDER_CAPACITY_BLOCKED",
                            dict(capacity_blocked), now_iso,
                            con=con,
                        )
                    )
                    # Fail closed: submit ZERO new intents.
                    order_intents = []
                elif existing_open + proposed > capacity_limit:
                    capacity_blocked = {
                        "existing_open_orders": existing_open,
                        "proposed_new_orders": proposed,
                        "max_open_orders": capacity_limit,
                        "total": existing_open + proposed,
                    }
                    events.append(
                        self._record_event(
                            db_path, cycle_id,
                            "OPEN_ORDER_CAPACITY_BLOCKED",
                            dict(capacity_blocked), now_iso,
                            con=con,
                        )
                    )
                    # Fail closed: submit ZERO new intents.
                    order_intents = []
                else:
                    # Patch 2D: every submission joins the single cycle
                    # transaction.  A failure on ANY order propagates out of
                    # this block and the whole cycle transaction rolls back,
                    # so no earlier order/reservation/accounting mutation from
                    # this cycle survives (Test 1: second-order failure).
                    for intent in order_intents:
                        existing_order = self.order_engine.get(
                            intent.client_order_id, con=con,
                        )
                        if existing_order is not None:
                            orders_skipped += 1
                            continue
                        self.order_engine.submit(
                            intent, cycle_input.risk_decision,
                            cycle_input.lower_price,
                            cycle_input.upper_price,
                            con=con,
                        )
                        orders_submitted += 1
                        events.append(
                            self._record_event(
                                db_path, cycle_id, "ORDER_SUBMITTED",
                                {"client_order_id": intent.client_order_id,
                                 "side": intent.side,
                                 "price": str(intent.price),
                                 "quantity": str(intent.quantity)},
                                now_iso,
                                con=con,
                            )
                        )

        # Step 9: Fill processing — deterministic price comparison.
        # Patch 2D: open orders are read through the cycle connection so
        # freshly-submitted (still uncommitted) orders are seen, and any
        # fill failure propagates out to roll back the whole cycle
        # transaction (Test 3: fill failure rolls back earlier fills).
        open_orders = self._get_open_orders(db_path, con=con)
        for order_data in open_orders:
            client_order_id = order_data["client_order_id"]
            side = order_data["side"]
            order_price = Decimal(order_data["price"])
            remaining = (
                Decimal(order_data["quantity"])
                - Decimal(order_data["executed_qty"])
            )
            # Fill identity: bound to order + fill semantics only.
            # EXCLUDES cycle_id (cycle identity must not determine fill
            # deduplication — a restart at the same candle must replay
            # the same fill_id, not generate a new one).
            # A SHA-256-12 suffix keeps the id within the _FILL_ID_RE
            # character set ([A-Za-z0-9_-]{1,64}) while remaining
            # fully deterministic on (client_order_id, price, quantity).
            fill_id = "fill_" + client_order_id + "_" + hashlib.sha256(
                f"{client_order_id}|{cycle_input.current_price}|{remaining}"
                .encode()
            ).hexdigest()[:12]

            if remaining <= Decimal("0"):
                fills_skipped += 1
                continue

            # Deterministic fill check: BUY fills when price <= order price,
            # SELL fills when price >= order price.
            if (side == "BUY" and cycle_input.current_price <= order_price) or (
                side == "SELL" and cycle_input.current_price >= order_price
            ):
                result = self.order_engine.apply_fill(
                    client_order_id, fill_id, cycle_input.symbol,
                    cycle_input.current_price, remaining,
                    fee_rate=cycle_input.maker_fee,
                    fee_asset=cycle_input.fee_asset,
                    con=con,
                )
                if result.applied:
                    fills_applied += 1
                    events.append(
                        self._record_event(
                            db_path, cycle_id, "ORDER_FILLED",
                            {"client_order_id": client_order_id,
                             "side": side,
                             "fill_price": str(cycle_input.current_price),
                             "quantity": str(remaining)}, now_iso,
                            con=con,
                        )
                    )
                elif result.idempotent:
                    fills_idempotent += 1
            else:
                fills_skipped += 1

        # Step 11: Recovery — validate post-mutation state consistency
        # through the cycle connection so reconciliation sees the uncommitted
        # cycle results (orders, fills, reservations, accounting).
        #
        # PATCH 5A: an UNHEALTHY post-mutation state MUST fail the whole cycle
        # transaction.  We record a RECOVERY_FAILED diagnostic event (which is
        # rolled back with the cycle) and then raise a typed recovery exception
        # so the outer cycle_transaction rolls back atomically — no order,
        # fill, reservation, accounting, lifecycle, or cycle-record mutation
        # survives.  The recovery failure is never swallowed.
        recovery_healthy = True
        try:
            recovery = self.order_engine.reconcile(con=con)
            if not recovery.healthy:
                recovery_healthy = False
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECOVERY_FAILED",
                        {"errors": [str(e) for e in recovery.errors],
                         "warnings": [str(w) for w in recovery.warnings]}, now_iso,
                        con=con,
                    )
                )
                # Hard fail: force the cycle transaction to roll back.
                recovery.raise_if_unhealthy()
            else:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECOVERY_OK", {}, now_iso,
                        con=con,
                    )
                )
        except PaperStateUnhealthyError as exc:
            # reconcile() surfaced a pre-cached unhealthy state — the same hard
            # veto: record the diagnostic, then re-raise so the cycle rolls
            # back rather than committing a corrupt state.  Never swallowed.
            recovery_healthy = False
            events.append(
                self._record_event(
                    db_path, cycle_id, "RECOVERY_FAILED",
                    {"error": str(exc)}, now_iso,
                    con=con,
                )
            )
            raise

        # Step 12: Re-read state for immutable snapshot (through the cycle
        # connection so the snapshot reflects post-mutation cycle state).
        accounting_state = self._read_accounting_state(db_path, con=con)
        orders = self._reconstruct_orders(db_path, con=con)
        open_filled_count = sum(
            1 for o in orders
            if o["status"] in ("OPEN", "PARTIALLY_FILLED")
        )
        filled_count = sum(
            1 for o in orders if o["status"] == "FILLED"
        )
        blocked_reason = None
        if plan_decision == PlanDecision.GRID_BLOCKED:
            if plan and plan.reasons:
                blocked_reason = "|".join(r.value for r in plan.reasons)
            else:
                blocked_reason = "PLANNER_ERROR"
        elif plan_decision is None:
            blocked_reason = "NO_PLAN_EVALUATED"

        # Allocation block / capacity block → deterministic blocked_reason.
        if allocation_blocked is not None:
            alloc_reasons = allocation_blocked.get("reasons", [])
            block_str = "|".join(alloc_reasons) if alloc_reasons else "UNKNOWN"
            blocked_reason = f"ALLOCATION_BLOCKED:{block_str}"
        elif capacity_blocked is not None:
            # PATCH 5B: an unresolved limit is reported deterministically,
            # distinct from a resolvable limit that was simply exceeded.
            if capacity_blocked.get("status") == "OPEN_ORDER_CAPACITY_UNRESOLVED":
                blocked_reason = "OPEN_ORDER_CAPACITY_UNRESOLVED"
            else:
                blocked_reason = (
                    f"OPEN_ORDER_CAPACITY_BLOCKED:"
                    f"existing={capacity_blocked['existing_open_orders']},"
                    f"proposed={capacity_blocked['proposed_new_orders']},"
                    f"max={capacity_blocked['max_open_orders']}"
                )

        # Lifecycle integrity block → deterministic blocked_reason (Patch 2C).
        # This takes precedence over allocation/capacity reasons because the
        # cycle was hard-vetted before any order generation occurred.
        if gate_blocked is not None:
            gate_code = gate_blocked.get("code", "LIFECYCLE_INVALID")
            gate_reasons = gate_blocked.get("reasons", [])
            gate_str = "|".join(gate_reasons) if gate_reasons else "UNKNOWN"
            blocked_reason = f"LIFECYCLE_BLOCKED:{gate_code}:{gate_str}"

        # Cycle success: allocation/capacity/lifecycle block → success=False.
        success = (
            allocation_blocked is None
            and capacity_blocked is None
            and gate_blocked is None
        )

        # Step 13: Record cycle (joins the cycle transaction; persists only
        # if the whole cycle commits).
        events.append(
            self._record_event(
                db_path, cycle_id, "CYCLE_COMPLETED",
                {"orders_submitted": orders_submitted,
                 "fills_applied": fills_applied,
                 "recovery_healthy": recovery_healthy}, now_iso,
                con=con,
            )
        )

        self._record_cycle(
            db_path, cycle_id, cycle_input.candle_index,
            cycle_input.symbol, plan_id, plan_decision,
            lifecycle_transition, orders_submitted, orders_skipped,
            fills_applied, fills_idempotent, fills_skipped,
            recovery_healthy, False, success, blocked_reason, None,
            cycle_input.metadata, now_iso,
            con=con,
        )

        # Step 14-15: Assemble and return immutable result
        return PaperCycleResult(
            cycle_id=cycle_id,
            candle_index=cycle_input.candle_index,
            symbol=cycle_input.symbol,
            plan_decision=plan_decision,
            plan=plan,
            lifecycle_transition=lifecycle_transition,
            order_intents=tuple(order_intents),
            orders_submitted=orders_submitted,
            orders_skipped=orders_skipped,
            fills_applied=fills_applied,
            fills_idempotent=fills_idempotent,
            fills_skipped=fills_skipped,
            accounting_state=accounting_state,
            recovery_healthy=recovery_healthy,
            events=tuple(events),
            blocked_reason=blocked_reason,
            success=success,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_open_order_capacity_limit(
        self, cycle_input: PaperCycleInput,
    ) -> int | None:
        """Compute the authoritative open-order capacity limit.

        Reuses the same limit semantics as symbol_rules._plan_limit:
        the minimum of the configured max_open_orders and the exchange's
        SymbolRules.max_num_orders (when > 0).  Returns None when no limit
        can be resolved (rules is None AND configured max_open_orders is
        missing/zero).

        PATCH 5B: a ``None`` return is NEVER treated as "unlimited".  The
        capacity gate in ``_run_cycle_txn`` blocks the cycle with the
        deterministic reason ``OPEN_ORDER_CAPACITY_UNRESOLVED`` when this
        returns None, so an unresolved limit fails closed (zero intents).
        """
        exec_cfg = cycle_input.cfg.get("execution", {})
        configured_max = int(exec_cfg.get("max_open_orders", 0) or 0)
        limits: list[int] = []
        if configured_max > 0:
            limits.append(configured_max)
        rules = cycle_input.rules
        if rules is not None and rules.max_num_orders > 0:
            limits.append(int(rules.max_num_orders))
        if not limits:
            return None
        return min(limits)

    def _get_open_order_ids(self, db_path: str, con=None) -> tuple[str, ...]:
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
        try:
            rows = con.execute(
                "SELECT client_order_id FROM orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchall()
            return tuple(r["client_order_id"] for r in rows)
        finally:
            if owns:
                con.close()

    def _get_open_orders(self, db_path: str, con=None) -> list[dict]:
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
        try:
            rows = con.execute(
                "SELECT * FROM orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            if owns:
                con.close()

    def _reconstruct_orders(self, db_path: str, con=None) -> list[dict]:
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
        try:
            rows = con.execute("SELECT * FROM orders").fetchall()
            return [dict(r) for r in rows]
        finally:
            if owns:
                con.close()

    def _read_accounting_state(self, db_path: str, con=None) -> dict | None:
        owns = con is None
        if owns:
            from storage import connect
            con = connect(db_path)
        try:
            row = con.execute(
                "SELECT * FROM paper_account_state LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            if owns:
                con.close()

    def _compute_planner_base_inventory(
        self, db_path: str, cycle_input: PaperCycleInput, con=None,
    ) -> Decimal:
        """Base inventory for the adaptive planner.

        Preferred source: the CURRENT paper accounting state
        (base_free + base_reserved), which reflects fills and order
        reservations.  Falls back to the configured
        initial_base_balance only when no accounting state row exists yet
        (first cycle before any submission/fill).

        Using live accounting instead of the stale initial config ensures
        the planner's SELL-level inventory check (_allocate_budget) is based
        on actual available base, not a frozen starting balance.
        """
        state = self._read_accounting_state(db_path, con=con)
        if state is not None:
            base_free = Decimal(str(state["base_free"]))
            base_reserved = Decimal(str(state["base_reserved"]))
            return base_free + base_reserved
        return Decimal(
            str(cycle_input.cfg.get("paper", {}).get(
                "initial_base_balance", "2"
            ))
        )

    def _get_cycle_events(
        self, db_path: str, cycle_id: str,
    ) -> list[PaperCycleEvent]:
        from storage import connect
        con = connect(db_path)
        try:
            rows = con.execute(
                "SELECT * FROM paper_orch_events WHERE cycle_id = ? "
                "ORDER BY created_at",
                (cycle_id,),
            ).fetchall()
            return [
                PaperCycleEvent(
                    event_id=r["event_id"],
                    cycle_id=r["cycle_id"],
                    event_type=r["event_type"],
                    payload=json.loads(r["payload"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]
        finally:
            con.close()

    def _generate_order_intents(
        self,
        cycle_input: PaperCycleInput,
        plan: AdaptiveGridPlan,
        clock: Any,
        accounting_state: dict | None,
        con=None,
        lc_prefix: str = "",
    ) -> _AllocationResult:
        """Generate BUY and SELL order intents from plan levels.

        Integrates Phase 5C inventory-aware allocation:
        - Build InventorySnapshot from accounting_state + current_price
        - Call allocate_grid() to determine which cells are fundable
        - Only generate intents for allowed (actionable) cells
        - Skip entirely if allocation cannot execute

        When ``con``/``lc_prefix`` are supplied (cycle-transaction join) the
        lifecycle generation and accounting reads go through the caller's
        connection so the allocation uses the cycle's in-transaction
        generation (bound to the just-mutated lifecycle) and post-mutation
        accounting state.

        Returns an _AllocationResult carrying both the intents and, when the
        allocation was blocked/failed, deterministic diagnostics so the
        caller can emit an ALLOCATION_BLOCKED cycle event.
        """
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # Guard: SymbolRules required for allocation quantization
        if cycle_input.rules is None:
            logger.warning(
                "No SymbolRules provided; skipping order intent generation"
            )
            block_info = {
                "status": "NO_RULES",
                "reasons": ["MISSING_SYMBOL_RULES"],
                "plan_id": plan.plan_id,
                "generation": None,
            }
            return _AllocationResult(intents=[], allocation_blocked=block_info)

        # Step 1: Build InventorySnapshot from accounting state dict
        if accounting_state is None:
            accounting_state = self._read_accounting_state(
                self.order_engine.db_path
            )
        if accounting_state is None:
            # No accounting state yet — treat as zero-balance
            accounting_state = {
                "base_asset": cycle_input.rules.base_asset,
                "quote_asset": cycle_input.rules.quote_asset,
                "base_free": "0",
                "base_reserved": "0",
                "quote_free": "0",
                "quote_reserved": "0",
                "average_cost": "0",
                "realized_pnl": "0",
                "total_fees": "0",
            }

        snapshot = InventorySnapshot.from_paper_state(
            accounting_state, cycle_input.current_price
        )

        # Step 2: Determine lifecycle state and generation for allocation gating
        # (through the cycle connection when joined, so a just-mutated
        # lifecycle generation is used, not the stale committed one).
        lifecycle_state = self.lifecycle_manager.get_current_state(
            con=con, prefix=lc_prefix,
        )
        # Map grid_lifecycle LifecycleState → inventory_model LifecyleState
        alloc_lifecycle = LifecycleState(lifecycle_state.value)

        current_generation = self.lifecycle_manager.get_generation(
            con=con, prefix=lc_prefix,
        )
        # Use the plan's plan_id; generation comes from lifecycle tracking
        plan_generation = current_generation

        # Step 3: Run deterministic allocation
        allocation = None
        try:
            allocation = allocate_grid(
                plan_id=plan.plan_id,
                generation=plan_generation,
                levels=tuple(plan.levels),
                snapshot=snapshot,
                rules=cycle_input.rules,
                cfg=cycle_input.cfg,
                lifecycle_state=alloc_lifecycle,
                expected_generation=current_generation,
                risk_allowed=cycle_input.risk_decision.allowed,
            )
        except Exception as exc:
            logger.error(
                "Allocation computation failed for %s: %s", plan.plan_id, exc,
            )
            block_info = {
                "status": "EXCEPTION",
                "reasons": [f"ALLOCATION_EXCEPTION:{exc}"],
                "plan_id": plan.plan_id,
                "generation": plan_generation,
            }
            return _AllocationResult(intents=[], allocation_blocked=block_info)

        # Step 4: Check allocation executability.
        # Distinguish:
        #  A. allocation VALID but zero actionable cells → NOT a block
        #  B. allocation not executable (blocked/invalid) → explicit block
        if not can_execute_allocation(allocation):
            reason_codes = [r.value for r in allocation.reason_codes]
            logger.info(
                "Allocation for plan %s not executable: status=%s, reasons=%s",
                plan.plan_id,
                allocation.status.value,
                reason_codes,
            )
            block_info = {
                "status": allocation.status.value,
                "reasons": reason_codes,
                "plan_id": plan.plan_id,
                "generation": plan_generation,
            }
            return _AllocationResult(intents=[], allocation_blocked=block_info)

        # Step 5: Filter to actionable cells only
        buy_cells, sell_cells = filter_actionable_cells(allocation)

        # Step 5b (FIX 4A): post-quantization net profit hard gate.
        #
        # The planner's profit gate runs on the PRE-quantization grid step.
        # Before any executable intent is emitted, recompute the ACTUAL
        # round-trip net profit from the exact tick-quantized execution
        # prices — the same quantize_price + net_pct_from_prices helpers that
        # main.py's validate_quantized_order_plan uses — and reject any cell
        # whose post-quantization profit is below the configured hard
        # minimum.  Each rejected cell records a deterministic reason; the
        # pre-quantization profit is never used as the final authority and
        # nothing is silently skipped.
        fee_cfg = cycle_input.cfg.get("fees", {})
        buy_fee = Decimal(str(cycle_input.maker_fee))
        sell_fee = (
            Decimal(str(cycle_input.maker_fee))
            if cycle_input.cfg.get("execution", {}).get("prefer_limit_maker", True)
            else Decimal(str(cycle_input.taker_fee))
        )
        slippage = Decimal(str(fee_cfg.get("slippage_roundtrip_pct", "0.0005")))
        hard_min = Decimal(
            str(cycle_input.cfg.get("grid", {}).get("hard_min_net_pct", "0.003"))
        )
        level_prices = [lv.price for lv in plan.levels]

        def _cell_profit(index: int, side: str) -> Decimal | None:
            """Post-quantization round-trip net pct for a cell, or None.

            BUY cell i fills round-trip against level i+1 (its sell side);
            SELL cell j against level j-1 (its buy side).  Out-of-range
            indices → None (fail closed → cell blocked).
            """
            n_levels = len(level_prices)
            if index < 0 or index >= n_levels:
                return None
            if side == "BUY":
                if index + 1 >= n_levels:
                    return None
                raw_buy, raw_sell = level_prices[index], level_prices[index + 1]
            else:
                if index - 1 < 0:
                    return None
                raw_buy, raw_sell = level_prices[index - 1], level_prices[index]
            return _post_quant_net_profit(
                raw_buy, raw_sell, cycle_input.rules,
                buy_fee, sell_fee, slippage,
            )

        def _gate_blocked(side: str, index: int) -> bool:
            net = _cell_profit(index, side)
            # net is None (quantization/profit computation failed, missing
            # level pair) OR below the hard minimum → cell is not executable.
            return net is None or net < hard_min

        # Step 6: Generate intents from allowed cells.
        # Quantity is taken directly from the allocation result (cell.quantity),
        # which is already quantized and fee-buffered by allocate_grid.
        order_type = (
            "LIMIT_MAKER"
            if cycle_input.cfg.get("execution", {}).get(
                "prefer_limit_maker", True
            )
            else "LIMIT"
        )

        intents: list[OrderIntent] = []
        profit_blocked_cells: list[str] = []

        for cell in buy_cells:
            # cell.quantity is already quantized/validated by allocate_grid
            if cell.quantity <= Decimal("0"):
                continue
            # FIX 4A: gate on the ACTUAL post-quantization economics.
            if _gate_blocked("BUY", cell.index):
                profit_blocked_cells.append(
                    f"POST_QUANTIZATION_PROFIT_BELOW_MIN:BUY:{cell.index}"
                )
                continue
            cid = make_client_order_id(
                self.client_order_prefix, cycle_input.symbol,
                plan_generation, cell.index, "BUY",
            )
            intents.append(OrderIntent(
                client_order_id=cid,
                symbol=cycle_input.symbol,
                side="BUY",
                order_type=order_type,
                price=cell.buy_price,
                quantity=cell.quantity,
                time_in_force="GTC",
                grid_index=cell.index,
                generation=plan_generation,
                created_at=now,
            ))

        for cell in sell_cells:
            # cell.quantity is already quantized/validated by allocate_grid
            if cell.quantity <= Decimal("0"):
                continue
            # FIX 4A: gate on the ACTUAL post-quantization economics.
            if _gate_blocked("SELL", cell.index):
                profit_blocked_cells.append(
                    f"POST_QUANTIZATION_PROFIT_BELOW_MIN:SELL:{cell.index}"
                )
                continue
            cid = make_client_order_id(
                self.client_order_prefix, cycle_input.symbol,
                plan_generation, cell.index, "SELL",
            )
            intents.append(OrderIntent(
                client_order_id=cid,
                symbol=cycle_input.symbol,
                side="SELL",
                order_type=order_type,
                price=cell.sell_price,
                quantity=cell.quantity,
                time_in_force="GTC",
                grid_index=cell.index,
                generation=plan_generation,
                created_at=now,
            ))

        # FIX 4A: every actionable cell rejected by the post-quantization
        # profit gate → deterministic whole-cycle block (zero submissions),
        # surfaced through the existing ALLOCATION_BLOCKED machinery so the
        # cycle records success=False + a deterministic blocked_reason.
        if profit_blocked_cells and not intents:
            return _AllocationResult(
                intents=[],
                allocation_blocked={
                    "status": "PROFIT_GATE_BLOCKED",
                    "reasons": tuple(profit_blocked_cells),
                    "plan_id": plan.plan_id,
                    "generation": plan_generation,
                },
                profit_blocked_cells=tuple(profit_blocked_cells),
            )

        # Valid allocation (even with zero actionable cells) → no block.
        # Cells dropped by the profit gate (partial case) are reported
        # deterministically via profit_blocked_cells.
        return _AllocationResult(
            intents=intents,
            allocation_blocked=None,
            profit_blocked_cells=tuple(profit_blocked_cells),
        )

    # ------------------------------------------------------------------
    # Recovery / state inspection
    # ------------------------------------------------------------------

    def recover(self):
        """Run recovery and return result."""
        return self.order_engine.reconcile()

    def is_healthy(self) -> bool:
        """Check if paper state is healthy."""
        try:
            result = self.order_engine.reconcile()
            return result.healthy
        except Exception:
            return False


# ---------------------------------------------------------------------------
# PaperSession: public API
# ---------------------------------------------------------------------------

class PaperSession:
    """Public API for deterministic paper-trading cycles.

    Not a daemon — the caller controls when to run each cycle.
    Each call to run_cycle() executes exactly one deterministic cycle.

    Usage:
        session = PaperSession(order_db_path, accounting_engine, ...)
        result = session.run_cycle(cycle_input)
    """

    def __init__(
        self,
        order_db_path: str,
        lifecycle_db_path: str,
        accounting_engine: PaperAccountingEngine,
        client_order_prefix: str = "AG",
    ):
        self.order_engine = PaperOrderEngine(
            order_db_path,
            client_order_prefix=client_order_prefix,
            accounting=accounting_engine,
        )
        self.orchestrator = PaperOrchestrator(
            lifecycle_db_path=lifecycle_db_path,
            order_db_path=order_db_path,
            accounting_engine=accounting_engine,
            order_engine=self.order_engine,
            client_order_prefix=client_order_prefix,
        )

    def run_cycle(self, cycle_input: PaperCycleInput) -> PaperCycleResult:
        """Execute a single deterministic paper-trading cycle."""
        return self.orchestrator.run_cycle(cycle_input)

    def is_healthy(self) -> bool:
        """Check if paper state is healthy."""
        return self.orchestrator.is_healthy()
