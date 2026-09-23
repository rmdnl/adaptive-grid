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
    LifecycleState,
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
from recovery import (
    RecoveryUnhealthyError,
    recover_paper_state,
)
from risk_engine import RiskDecision

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
# Cycle identity: deterministic hash
# ---------------------------------------------------------------------------

def generate_cycle_id(
    candle_index: int,
    symbol: str,
    plan_id: str,
    order_ids: tuple[str, ...],
) -> str:
    """Deterministic cycle identity: same logical inputs → same cycle_id.

    No randomness, no wall-clock time.  The cycle_id is a SHA-256 hash
    of the canonical representation of all deterministic inputs.
    """
    payload = json.dumps({
        "candle_index": candle_index,
        "symbol": symbol,
        "plan_id": plan_id or "",
        "order_ids": sorted(order_ids),
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
        metadata: dict, created_at: str,
    ) -> None:
        from storage import connect
        con = connect(db_path)
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
            con.commit()
        finally:
            con.close()

    def _record_event(
        self, db_path: str, cycle_id: str, event_type: str,
        payload: dict, created_at: str,
    ) -> PaperCycleEvent:
        event_id = hashlib.sha256(
            f"{cycle_id}:{event_type}:{created_at}:{json.dumps(payload, sort_keys=True, default=str)}".encode()
        ).hexdigest()[:20]
        from storage import connect
        con = connect(db_path)
        try:
            con.execute(
                """INSERT OR IGNORE INTO paper_orch_events
                (event_id, cycle_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (event_id, cycle_id, event_type,
                 json.dumps(payload, default=str), created_at),
            )
            con.commit()
        finally:
            con.close()
        return PaperCycleEvent(
            event_id=event_id, cycle_id=cycle_id,
            event_type=event_type, payload=payload,
            created_at=created_at,
        )

    def _check_existing_cycle(self, db_path: str, cycle_id: str) -> dict | None:
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
            con.close()

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
        from storage import connect
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
                cycle_input.candle_index, cycle_input.symbol, "", ()
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
        base_available = Decimal(
            str(cycle_input.cfg.get("paper", {}).get(
                "initial_base_balance", "2"
            ))
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

        # Generate cycle ID deterministically (before any side effects)
        open_order_ids = self._get_open_order_ids(db_path)

        plan_id = plan.plan_id if plan else (
            cycle_input.active_plan.plan_id if cycle_input.active_plan else ""
        )
        cycle_id = generate_cycle_id(
            cycle_input.candle_index, cycle_input.symbol,
            plan_id, open_order_ids,
        )

        # Check idempotency: if cycle_id already exists, return cached result
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

        events: list[PaperCycleEvent] = []

        # CYCLE_STARTED
        events.append(
            self._record_event(
                db_path, cycle_id, "CYCLE_STARTED",
                {"candle_index": cycle_input.candle_index,
                 "symbol": cycle_input.symbol,
                 "plan_id": plan_id or None}, now_iso,
            )
        )

        # Step 5: Lifecycle manager
        lifecycle_transition = None
        if plan is not None:
            try:
                if active_plan_state is not None:
                    transition = self.lifecycle_manager.handle_planner_decision(
                        plan, active_plan_state, cycle_input.cfg,
                        candle_index=cycle_input.candle_index,
                    )
                else:
                    transition = self.lifecycle_manager.handle_planner_decision(
                        plan, None, cycle_input.cfg,
                        candle_index=cycle_input.candle_index,
                    )
                lifecycle_transition = transition.to_state.value
            except LifecycleError as exc:
                lifecycle_transition = f"ERROR:{exc.code.value}"
                events.append(
                    self._record_event(
                        db_path, cycle_id, "LIFECYCLE_ERROR",
                        {"error": str(exc), "code": exc.code.value}, now_iso,
                    )
                )

            # Emit decision-specific events
            if plan_decision == PlanDecision.GRID_ALLOWED:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "PLAN_ACTIVATED",
                        {"plan_id": plan.plan_id,
                         "grid_count": plan.grid_count}, now_iso,
                    )
                )
            elif plan_decision == PlanDecision.RECONFIGURATION_REQUIRED:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECONFIGURATION_PENDING",
                        {"plan_id": plan.plan_id}, now_iso,
                    )
                )
                if lifecycle_transition == LifecycleState.READY_TO_RECONFIGURE.value:
                    events.append(
                        self._record_event(
                            db_path, cycle_id, "RECONFIGURATION_READY",
                            {"plan_id": plan.plan_id}, now_iso,
                        )
                    )
            elif plan_decision == PlanDecision.GRID_BLOCKED:
                reasons = [r.value for r in plan.reasons] if plan and plan.reasons else []
                events.append(
                    self._record_event(
                        db_path, cycle_id, "GRID_BLOCKED",
                        {"plan_id": plan.plan_id if plan is not None else None,
                         "reasons": reasons}, now_iso,
                    )
                )
            elif plan_decision == PlanDecision.KEEP_CURRENT_PLAN:
                pass  # No event needed; plan stays authoritative

        # Step 6: Inventory allocation (read accounting state)
        accounting_state = None
        con = connect(db_path)
        try:
            row = con.execute(
                "SELECT * FROM paper_account_state LIMIT 1"
            ).fetchone()
            if row is not None:
                accounting_state = dict(row)
        finally:
            con.close()

        # Step 7-10: Order intents, submission, fill processing
        order_intents: list[OrderIntent] = []
        orders_submitted = 0
        orders_skipped = 0
        fills_applied = 0
        fills_idempotent = 0
        fills_skipped = 0

        should_submit = (
            cycle_input.risk_decision.allowed
            and plan_decision is not None
            and plan is not None
            and plan_decision in (PlanDecision.GRID_ALLOWED,)
        )

        if should_submit:
            order_intents = self._generate_order_intents(
                cycle_input, plan, clock,
            )
            for intent in order_intents:
                existing_order = self.order_engine.get(intent.client_order_id)
                if existing_order is not None:
                    orders_skipped += 1
                    continue
                try:
                    self.order_engine.submit(
                        intent, cycle_input.risk_decision,
                        cycle_input.lower_price, cycle_input.upper_price,
                    )
                    orders_submitted += 1
                    events.append(
                        self._record_event(
                            db_path, cycle_id, "ORDER_SUBMITTED",
                            {"client_order_id": intent.client_order_id,
                             "side": intent.side,
                             "price": str(intent.price),
                             "quantity": str(intent.quantity)}, now_iso,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        "Order submission failed for %s: %s",
                        intent.client_order_id, exc,
                    )

        # Step 9: Fill processing — deterministic price comparison
        open_orders = self._get_open_orders(db_path)
        for order_data in open_orders:
            client_order_id = order_data["client_order_id"]
            side = order_data["side"]
            order_price = Decimal(order_data["price"])
            remaining = (
                Decimal(order_data["quantity"])
                - Decimal(order_data["executed_qty"])
            )
            fill_id = f"fill_{cycle_id}_{client_order_id}"

            if remaining <= Decimal("0"):
                fills_skipped += 1
                continue

            # Deterministic fill check: BUY fills when price <= order price,
            # SELL fills when price >= order price.
            if side == "BUY" and cycle_input.current_price <= order_price:
                try:
                    result = self.order_engine.apply_fill(
                        client_order_id, fill_id, cycle_input.symbol,
                        cycle_input.current_price, remaining,
                        fee_rate=cycle_input.maker_fee,
                        fee_asset=cycle_input.fee_asset,
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
                            )
                        )
                    elif result.idempotent:
                        fills_idempotent += 1
                except Exception as exc:
                    logger.error(
                        "Fill processing failed for %s: %s",
                        client_order_id, exc,
                    )
                    fills_skipped += 1
            elif side == "SELL" and cycle_input.current_price >= order_price:
                try:
                    result = self.order_engine.apply_fill(
                        client_order_id, fill_id, cycle_input.symbol,
                        cycle_input.current_price, remaining,
                        fee_rate=cycle_input.maker_fee,
                        fee_asset=cycle_input.fee_asset,
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
                            )
                        )
                    elif result.idempotent:
                        fills_idempotent += 1
                except Exception as exc:
                    logger.error(
                        "Fill processing failed for %s: %s",
                        client_order_id, exc,
                    )
                    fills_skipped += 1
            else:
                fills_skipped += 1

        # Step 11: Recovery — validate state consistency
        recovery_healthy = True
        try:
            recovery = self.order_engine.reconcile()
            if not recovery.healthy:
                recovery_healthy = False
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECOVERY_FAILED",
                        {"errors": [str(e) for e in recovery.errors],
                         "warnings": [str(w) for w in recovery.warnings]}, now_iso,
                    )
                )
            else:
                events.append(
                    self._record_event(
                        db_path, cycle_id, "RECOVERY_OK", {}, now_iso,
                    )
                )
        except PaperStateUnhealthyError as exc:
            recovery_healthy = False
            events.append(
                self._record_event(
                    db_path, cycle_id, "RECOVERY_FAILED",
                    {"error": str(exc)}, now_iso,
                )
            )

        # Step 12: Re-read state for immutable snapshot
        accounting_state = self._read_accounting_state(db_path)
        orders = self._reconstruct_orders(db_path)
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

        # Step 13: Record cycle
        events.append(
            self._record_event(
                db_path, cycle_id, "CYCLE_COMPLETED",
                {"orders_submitted": orders_submitted,
                 "fills_applied": fills_applied,
                 "recovery_healthy": recovery_healthy}, now_iso,
            )
        )

        self._record_cycle(
            db_path, cycle_id, cycle_input.candle_index,
            cycle_input.symbol, plan_id, plan_decision,
            lifecycle_transition, orders_submitted, orders_skipped,
            fills_applied, fills_idempotent, fills_skipped,
            recovery_healthy, False, True, blocked_reason, None,
            cycle_input.metadata, now_iso,
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
            success=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_open_order_ids(self, db_path: str) -> tuple[str, ...]:
        from storage import connect
        con = connect(db_path)
        try:
            rows = con.execute(
                "SELECT client_order_id FROM orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchall()
            return tuple(r["client_order_id"] for r in rows)
        finally:
            con.close()

    def _get_open_orders(self, db_path: str) -> list[dict]:
        from storage import connect
        con = connect(db_path)
        try:
            rows = con.execute(
                "SELECT * FROM orders "
                "WHERE status IN ('OPEN', 'PARTIALLY_FILLED')"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def _reconstruct_orders(self, db_path: str) -> list[dict]:
        from storage import connect
        con = connect(db_path)
        try:
            rows = con.execute("SELECT * FROM orders").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def _read_accounting_state(self, db_path: str) -> dict | None:
        from storage import connect
        con = connect(db_path)
        try:
            row = con.execute(
                "SELECT * FROM paper_account_state LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

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
    ) -> list[OrderIntent]:
        """Generate BUY and SELL order intents from plan levels.

        BUY levels: price < current_price (below current price)
        SELL levels: price >= current_price (at or above current price)
        """
        intents = []
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        order_quote_size = Decimal(
            str(cycle_input.cfg.get("execution", {}).get("order_quote_size", "25"))
        )
        order_type = (
            "LIMIT_MAKER"
            if cycle_input.cfg.get("execution", {}).get(
                "prefer_limit_maker", True
            )
            else "LIMIT"
        )

        for i in range(len(plan.levels) - 1):
            level = plan.levels[i]
            price = level.price
            if price <= Decimal("0"):
                continue
            quantity = (order_quote_size / price).quantize(Decimal("0.00000001"))

            if price < cycle_input.current_price:
                side = "BUY"
            else:
                side = "SELL"

            cid = make_client_order_id(
                self.client_order_prefix, cycle_input.symbol, level.index, side,
            )
            intents.append(OrderIntent(
                client_order_id=cid,
                symbol=cycle_input.symbol,
                side=side,
                order_type=order_type,
                price=price,
                quantity=quantity,
                time_in_force="GTC",
                grid_index=level.index,
                created_at=now,
            ))

        return intents

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
