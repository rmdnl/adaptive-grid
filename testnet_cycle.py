"""Round 8 — bounded, controlled continuous TESTNET cycle harness.

Repeats the verified Round 7 order path as a bounded cycle:

    market data → indicators → range → grid → risk veto → order intents
    → LIMIT_MAKER placement (testnet only) → authoritative reconciliation
    → verified cancellation → cleanup proof

This module is a SEPARATE surface: it never touches ``main()``, the paper
engine, or the paper database.  The production cycle remains paper-only and
``main()`` still refuses to run with ``dry_run=false``.

Safety architecture (unchanged invariants):

* **Testnet only.**  Every client re-asserts the adapter barrier
  (``environment=testnet``, ``dry_run=true``, ``allow_live_execution=false``,
  base URL pinned to ``https://testnet.binance.vision``).
* **Explicit gate.**  Placement requires ``TESTNET_ORDERS_ENABLED=true``
  (default false).  Rehearsal mode (no gate) exercises the full cycle
  read-only and places nothing.
* **Risk Engine is the authoritative veto.**  The runner calls the exact
  production gate functions from ``risk_engine`` (range-break kill, 15m
  lower-boundary kill, 2% equity-drawdown kill against a persisted
  high-water reference, market filter, open-order capacity, per-cell
  minimum-net-profit, strict price-inside-range).  No risk logic is
  re-implemented here.
* **Deterministic outcomes.**  Every exchange interaction settles to
  CONFIRMED (validated ack), deterministic rejection (Binance error code),
  or UNKNOWN.  A POST is never retried; a lost submission ack is resolved
  by clientOrderId (§4) and never resubmitted; an ambiguous cancel is
  settled only by an authoritative re-query (§5).
* **Persistence.**  A separate SQLite ledger (``data/testnet_cycle.sqlite3``,
  PRAGMA user_version 800) records runs, cycles, orders (state machine incl.
  ``PENDING_RECONCILIATION``), events, the kill latch, and the reference
  equity high-water mark.  No existing database is migrated or touched.
* **Kill state.**  Latched FIRST (crash-safe, survives restart) on the 2%
  drawdown kill, range-break kill, or 15m lower-boundary kill; open orders
  are then canceled fail-closed; no new orders while latched.
* **Cleanup proof.**  At the end of a run: reconcile everything, cancel only
  confirmed-open own orders, re-resolve, and PROVE zero open own orders and
  zero non-terminal ledger orders.  Anything unprovable FAILS with the
  exact client order ids.  Foreign orders (different clientOrderId
  namespace) are never touched and are reported; their presence refuses
  placement (fail closed) but is outside this harness's authority.
* **Observability.**  Every decision, veto, submission, reconciliation,
  fill, cancel, error, and recovery event is persisted to ``cycle_events``
  and summarized in ``status()``.  No credentials ever enter events or
  logs (the adapter redacts at the boundary).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from binance_testnet import (
    BinanceTestnetClient,
    BinanceTestnetConfigError,
    BinanceTestnetError,
    BinanceTestnetNetworkError,
)
from indicators import enrich, latest_valid_row
from market_data import fetch_klines
from range_engine import auto_range
from grid_engine import build_geometric_grid
from rest_reconciler import CancelVerdict, Outcome, RestReconciler
from risk_engine import (
    combine,
    equity_dd_kill,
    equity_reference_gate,
    lower_boundary_15m_kill,
    market_gate,
    open_orders_gate,
    range_break_kill,
    strict_order_price_gate,
)
from shutdown import ShutdownCoordinator, run_loop_boundary_check
from symbol_rules import (
    SymbolRuleError,
    parse_symbol_info,
    validate_quantized_order_plan,
)
from testnet_orders import (
    BinanceTestnetOrderClient,
    BinanceTestnetOrderRejectedError,
)

logger_name = "testnet_cycle"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class TestnetCycleError(RuntimeError):
    """Base class for cycle-harness errors."""


class TestnetCycleConfigError(TestnetCycleError):
    """Configuration validation failed.  Fail-closed."""


class TestnetCycleSafetyError(TestnetCycleError):
    """A fail-closed refusal: the cycle must not place orders."""


# ---------------------------------------------------------------------------
# Order state machine
# ---------------------------------------------------------------------------
ORDER_INTENT = "INTENT"
ORDER_SUBMITTED_UNKNOWN = "SUBMITTED_UNKNOWN"
ORDER_OPEN = "OPEN"
ORDER_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORDER_FILLED = "FILLED"
ORDER_CANCELED = "CANCELED"
ORDER_REJECTED = "REJECTED"
ORDER_PENDING_RECONCILIATION = "PENDING_RECONCILIATION"

#: States that still may hold exchange exposure or unresolved outcomes.
NON_TERMINAL_STATES = (
    ORDER_INTENT,
    ORDER_SUBMITTED_UNKNOWN,
    ORDER_OPEN,
    ORDER_PARTIALLY_FILLED,
    ORDER_PENDING_RECONCILIATION,
)

#: Authoritative exchange statuses we accept for an order query.
_KNOWN_EXCHANGE_STATUSES = frozenset({
    "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED",
})


def is_terminal(state: str) -> bool:
    return state not in NON_TERMINAL_STATES


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TestnetCycleConfig:
    """Validated, bounded cycle configuration.

    Strategy/risk fields are sourced from the validated ``config.yaml`` dict
    (single source of truth — the same values the production risk gates
    read).  Cycle-boundedness fields are validated here.
    """

    symbol: str
    max_cycles: int
    poll_interval_s: float
    max_orders_per_cycle: int
    order_quote_size: Decimal
    db_path: str
    # strategy parameters (from config.yaml — not re-declared here)
    grid_step_pct: Decimal
    hard_min_net_pct: Decimal
    min_cells: int
    max_levels: int
    range_auto: dict
    range_break_buffer_pct: Decimal
    stop_if_below_lower_pct: Decimal
    max_equity_drawdown_pct: Decimal
    market_filter: dict
    maker_fee: Decimal
    slippage_roundtrip_pct: Decimal
    max_open_orders: int
    # cycle safety bounds
    clock_skew_max_ms: int
    balance_buffer: Decimal

    def __post_init__(self) -> None:
        def _need(cond: bool, msg: str) -> None:
            if not cond:
                raise TestnetCycleConfigError(msg)

        _need(isinstance(self.symbol, str) and self.symbol.strip(),
              "symbol must be a non-empty string")
        _need(int(self.max_cycles) == self.max_cycles and 1 <= self.max_cycles <= 100,
              "max_cycles must be an integer in [1, 100] (bounded, no 24/7 loop)")
        _need(self.poll_interval_s >= 0, "poll_interval_s must be >= 0")
        _need(int(self.max_orders_per_cycle) == self.max_orders_per_cycle
              and 1 <= self.max_orders_per_cycle <= 10,
              "max_orders_per_cycle must be an integer in [1, 10]")
        _need(self.order_quote_size > 0, "order_quote_size must be > 0")
        _need(isinstance(self.db_path, str) and self.db_path.strip(),
              "db_path must be a non-empty string")
        _need(self.grid_step_pct > 0, "grid_step_pct must be > 0")
        _need(self.hard_min_net_pct >= Decimal("0.003"),
              "hard_min_net_pct must be >= 0.003 (invariant)")
        _need(self.min_cells >= 1, "min_cells must be >= 1")
        _need(self.max_levels > self.min_cells, "max_levels must be > min_cells")
        _need(self.max_equity_drawdown_pct == Decimal("0.02"),
              "max_equity_drawdown_pct must remain 0.02 (invariant)")
        _need(self.range_break_buffer_pct == Decimal("0.01"),
              "range_break_buffer_pct must remain 0.01 (invariant)")
        _need(Decimal("0") < self.stop_if_below_lower_pct < Decimal("1"),
              "stop_if_below_lower_pct must be in (0, 1)")
        _need(int(self.max_open_orders) > 0, "max_open_orders must be > 0")
        _need(self.clock_skew_max_ms > 0, "clock_skew_max_ms must be > 0")
        _need(self.balance_buffer >= Decimal("1"),
              "balance_buffer must be >= 1")
        _need(isinstance(self.range_auto, dict) and self.range_auto,
              "range_auto must be a non-empty dict")
        _need(isinstance(self.market_filter, dict) and self.market_filter,
              "market_filter must be a non-empty dict")


def load_cycle_config(
    cfg: dict,
    *,
    symbol: Optional[str] = None,
    max_cycles: int = 3,
    poll_interval_s: float = 5.0,
    max_orders_per_cycle: int = 2,
    db_path: str = "./data/testnet_cycle.sqlite3",
    clock_skew_max_ms: int = 2000,
) -> TestnetCycleConfig:
    """Build the cycle config from a VALIDATED ``config_loader`` dict.

    Fails closed: the caller must run ``config_loader.validate_config``
    first; every locked parameter is passed through unchanged and is
    re-validated by :class:`TestnetCycleConfig`.
    """
    fees = cfg["fees"]
    return TestnetCycleConfig(
        symbol=str(symbol or cfg["symbol"]).upper(),
        max_cycles=int(max_cycles),
        poll_interval_s=float(poll_interval_s),
        max_orders_per_cycle=int(max_orders_per_cycle),
        order_quote_size=Decimal(str(cfg["execution"]["order_quote_size"])),
        db_path=db_path,
        grid_step_pct=Decimal(str(cfg["grid"]["step_pct"])),
        hard_min_net_pct=Decimal(str(cfg["grid"]["hard_min_net_pct"])),
        min_cells=int(cfg["grid"]["min_cells"]),
        max_levels=int(cfg["grid"]["max_levels"]),
        range_auto=dict(cfg["range"]["auto"]),
        range_break_buffer_pct=Decimal(str(cfg["risk"]["range_break_buffer_pct"])),
        stop_if_below_lower_pct=Decimal(str(cfg["risk"]["stop_if_below_lower_pct"])),
        max_equity_drawdown_pct=Decimal(str(cfg["risk"]["max_equity_drawdown_pct"])),
        market_filter=dict(cfg["market_filter"]),
        maker_fee=Decimal(str(fees["maker_fee_fallback"])),
        slippage_roundtrip_pct=Decimal(str(fees["slippage_roundtrip_pct"])),
        max_open_orders=int(cfg["execution"]["max_open_orders"]),
        clock_skew_max_ms=int(clock_skew_max_ms),
        balance_buffer=Decimal("1.01"),
    )


# ---------------------------------------------------------------------------
# Ledger (separate SQLite database — never touches the paper DB)
# ---------------------------------------------------------------------------
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cycle_runs ("
    " run_id INTEGER PRIMARY KEY,"
    " started_at_ms INTEGER NOT NULL,"
    " config_json TEXT NOT NULL,"
    " status TEXT NOT NULL DEFAULT 'RUNNING',"
    " detail TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS cycle_orders ("
    " client_order_id TEXT PRIMARY KEY,"
    " run_id INTEGER NOT NULL,"
    " cycle_id INTEGER NOT NULL,"
    " symbol TEXT NOT NULL,"
    " side TEXT NOT NULL,"
    " price TEXT NOT NULL,"
    " quantity TEXT NOT NULL,"
    " state TEXT NOT NULL,"
    " exchange_order_id INTEGER,"
    " fill_qty TEXT NOT NULL DEFAULT '0',"
    " detail TEXT NOT NULL DEFAULT '',"
    " created_at_ms INTEGER NOT NULL,"
    " updated_at_ms INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cycle_events ("
    " event_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " run_id INTEGER NOT NULL,"
    " cycle_id INTEGER NOT NULL,"
    " kind TEXT NOT NULL,"
    " payload_json TEXT NOT NULL,"
    " at_ms INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cycle_kill_state ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " active INTEGER NOT NULL,"
    " reason TEXT NOT NULL DEFAULT '',"
    " actor TEXT NOT NULL DEFAULT '',"
    " latched_at_ms INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cycle_bot_state ("
    " key TEXT PRIMARY KEY,"
    " value TEXT NOT NULL)",
)


class CycleLedger:
    """Persistent cycle journal (orders, events, kill latch, HWM)."""

    def __init__(self, path: str, *, clock_ms: Callable[[], int]) -> None:
        self._conn = sqlite3.connect(path)
        self._clock_ms = clock_ms
        self._conn.execute("PRAGMA user_version = 800")
        for stmt in _SCHEMA:
            self._conn.execute(stmt)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- runs ----------------------------------------------------------------
    def start_run(self, config_json: str) -> int:
        run_id = int(self._clock_ms())
        self._conn.execute(
            "INSERT OR REPLACE INTO cycle_runs (run_id, started_at_ms, config_json)"
            " VALUES (?, ?, ?)",
            (run_id, run_id, config_json),
        )
        self._conn.commit()
        return run_id

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        self._conn.execute(
            "UPDATE cycle_runs SET status = ?, detail = ? WHERE run_id = ?",
            (status, detail, run_id),
        )
        self._conn.commit()

    # -- events --------------------------------------------------------------
    def record_event(self, run_id: int, cycle_id: int, kind: str,
                     payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO cycle_events (run_id, cycle_id, kind, payload_json, at_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, cycle_id, kind, json.dumps(payload, sort_keys=True,
                                                default=str),
             self._clock_ms()),
        )
        self._conn.commit()

    def events(self, run_id: Optional[int] = None) -> list[dict]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT run_id, cycle_id, kind, payload_json, at_ms"
                " FROM cycle_events ORDER BY event_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT run_id, cycle_id, kind, payload_json, at_ms"
                " FROM cycle_events WHERE run_id = ? ORDER BY event_id",
                (run_id,)).fetchall()
        return [
            {"run_id": r[0], "cycle_id": r[1], "kind": r[2],
             "payload": json.loads(r[3]), "at_ms": r[4]}
            for r in rows
        ]

    # -- orders ----------------------------------------------------------------
    def insert_order(self, *, client_order_id: str, run_id: int, cycle_id: int,
                     symbol: str, side: str, price: Decimal, quantity: Decimal,
                     state: str) -> bool:
        """Insert a new order row.  False when the cid already exists
        (deterministic duplicate prevention at the ledger level)."""
        now = self._clock_ms()
        try:
            self._conn.execute(
                "INSERT INTO cycle_orders (client_order_id, run_id, cycle_id,"
                " symbol, side, price, quantity, state, created_at_ms,"
                " updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client_order_id, run_id, cycle_id, symbol.upper(), side,
                 str(price), str(quantity), state, now, now),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.commit()
        return True

    def update_order(self, client_order_id: str, *, state: Optional[str] = None,
                     exchange_order_id: Optional[int] = None,
                     fill_qty: Optional[Decimal] = None,
                     detail: Optional[str] = None) -> None:
        sets, vals = ["updated_at_ms = ?"], [self._clock_ms()]
        if state is not None:
            sets.append("state = ?")
            vals.append(state)
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?")
            vals.append(int(exchange_order_id))
        if fill_qty is not None:
            sets.append("fill_qty = ?")
            vals.append(str(fill_qty))
        if detail is not None:
            sets.append("detail = ?")
            vals.append(detail)
        vals.append(client_order_id)
        self._conn.execute(
            f"UPDATE cycle_orders SET {', '.join(sets)} WHERE client_order_id = ?",
            vals,
        )
        self._conn.commit()

    def get_order(self, client_order_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT client_order_id, run_id, cycle_id, symbol, side, price,"
            " quantity, state, exchange_order_id, fill_qty, detail"
            " FROM cycle_orders WHERE client_order_id = ?",
            (client_order_id,)).fetchone()
        return self._order_row(row) if row else None

    def orders_in_states(self, *states: str) -> list[dict]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        rows = self._conn.execute(
            "SELECT client_order_id, run_id, cycle_id, symbol, side, price,"
            " quantity, state, exchange_order_id, fill_qty, detail"
            f" FROM cycle_orders WHERE state IN ({placeholders})"
            " ORDER BY created_at_ms",
            tuple(states)).fetchall()
        return [self._order_row(r) for r in rows]

    def all_orders(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT client_order_id, run_id, cycle_id, symbol, side, price,"
            " quantity, state, exchange_order_id, fill_qty, detail"
            " FROM cycle_orders ORDER BY created_at_ms").fetchall()
        return [self._order_row(r) for r in rows]

    @staticmethod
    def _order_row(row) -> dict:
        return {
            "client_order_id": row[0], "run_id": row[1], "cycle_id": row[2],
            "symbol": row[3], "side": row[4], "price": row[5],
            "quantity": row[6], "state": row[7], "exchange_order_id": row[8],
            "fill_qty": row[9], "detail": row[10],
        }

    # -- kill latch ------------------------------------------------------------
    def kill_active(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT active, reason, actor, latched_at_ms FROM cycle_kill_state"
            " WHERE id = 1").fetchone()
        if row is None or not row[0]:
            return None
        return {"reason": row[1], "actor": row[2], "latched_at_ms": row[3]}

    def latch_kill(self, reason: str, actor: str) -> None:
        """Latch FIRST — the persisted latch exists even if the process dies
        before the cancel pass completes."""
        self._conn.execute(
            "INSERT INTO cycle_kill_state (id, active, reason, actor, latched_at_ms)"
            " VALUES (1, 1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET active = 1, reason = ?, actor = ?,"
            " latched_at_ms = ?",
            (reason, actor, self._clock_ms(), reason, actor, self._clock_ms()),
        )
        self._conn.commit()

    # -- key/value state --------------------------------------------------------
    def get_state(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM cycle_bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO cycle_bot_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def delete_state(self, key: str) -> None:
        self._conn.execute("DELETE FROM cycle_bot_state WHERE key = ?", (key,))
        self._conn.commit()


def _decimal_or_none(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class TestnetCycleRunner:
    """Bounded continuous testnet cycle (see module docstring)."""

    def __init__(
        self,
        config: TestnetCycleConfig,
        *,
        read_client: BinanceTestnetClient,
        order_client: Optional[BinanceTestnetOrderClient] = None,
        kline_rest_api: Any = None,
        ledger: Optional[CycleLedger] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: Optional[ShutdownCoordinator] = None,
        config_json: str = "{}",
    ) -> None:
        self.config = config
        self.read_client = read_client
        self.order_client = order_client
        self._kline_rest_api = kline_rest_api
        self.ledger = ledger or CycleLedger(
            config.db_path, clock_ms=lambda: int(clock() * 1000)
        )
        self.clock = clock
        self.sleep = sleep
        self.shutdown = shutdown or ShutdownCoordinator()
        self._rules = None  # parsed SymbolRules (preflight)
        # Reconciler: read path always; the cancel executor exists only when
        # a write-capable client is provided (rehearsal keeps the dry
        # executor, which settles UNRECONCILED without network calls).
        if order_client is not None:
            def _resolver(sym: str, cid: str):
                payload = read_client.get_order(sym, cid)
                return payload.get("status")
            executor = order_client.make_cancel_executor(resolver=_resolver)
        else:
            executor = None
        self.reconciler = RestReconciler(
            read_client, cancel_executor=executor, sleep=sleep
        )
        self.run_id: Optional[int] = None

    # -- helpers -------------------------------------------------------------
    def _event(self, cycle_id: int, kind: str, **payload) -> None:
        self.ledger.record_event(self.run_id or 0, cycle_id, kind, payload)

    def _cid(self, cycle_id: int, seq: int) -> str:
        return (
            f"AGTC-{self.config.symbol}-{self.run_id}-{cycle_id}-{seq}"
        )

    # -- preflight -------------------------------------------------------------
    def preflight(self) -> dict:
        """Fail-closed preconditions; returns a status report.

        Any exchange-side failure here is a fail-closed refusal (never a
        leak): the caller must not place orders on unverified state.
        """
        report: dict[str, Any] = {}
        try:
            skew = self.reconciler.sync_clock_skew(samples=3)
            if not skew.established:
                raise TestnetCycleSafetyError(
                    f"clock sync failed: {skew.detail}")
            if abs(skew.offset_ms) > self.config.clock_skew_max_ms:
                raise TestnetCycleSafetyError(
                    f"clock skew |{skew.offset_ms}ms| exceeds bound "
                    f"{self.config.clock_skew_max_ms}ms (fail closed)")
            report["clock_skew_ms"] = skew.offset_ms

            snapshot = self.read_client.symbol_snapshot(self.config.symbol)
            if snapshot.status.upper() != "TRADING":
                raise TestnetCycleSafetyError(
                    f"symbol {self.config.symbol} status {snapshot.status!r}")
            self._rules = parse_symbol_info(snapshot.raw_exchange_info)
            report["tick_size"] = str(self._rules.tick_size)
            report["step_size"] = str(self._rules.step_size)
            report["min_notional"] = str(self._rules.min_notional)

            remote = self.read_client.open_orders(self.config.symbol)
        except BinanceTestnetError as exc:
            raise TestnetCycleSafetyError(
                f"preflight exchange failure: {type(exc).__name__} "
                "(fail closed)") from exc
        prefix = f"AGTC-{self.config.symbol}-"
        foreign = [o.client_order_id for o in remote
                   if not o.client_order_id.startswith(prefix)]
        report["foreign_open_orders"] = foreign
        report["own_open_orders"] = [
            o.client_order_id for o in remote
            if o.client_order_id.startswith(prefix)]
        report["kill_active"] = self.ledger.kill_active()
        return report

    # -- market data / range / grid (production math, read-only) -----------------
    def _market_cycle_data(self) -> dict:
        if self._kline_rest_api is None:
            # Reuse the validated read client's underlying REST API — the
            # testnet URL and credentials were already barrier-checked.
            self._kline_rest_api = self.read_client._spot.rest_api
        if getattr(self, "_kline_client", None) is None:
            self._kline_client = type(
                "_KlineClient", (), {"rest_api": self._kline_rest_api})()
        klines = fetch_klines(
            self._kline_client,
            self.config.symbol,
            interval="15m",
            limit=200,
            drop_incomplete=True,
        )
        enriched = enrich(klines)
        ticker = self.read_client.ticker_price(self.config.symbol)
        candidate = auto_range(enriched, **self.config.range_auto)
        return {"enriched": enriched, "ticker": ticker, "range": candidate}

    def _risk_decision(self, data: dict, plan_valid, remote_open: int):
        """Combine the EXACT production risk gates (no re-implemented logic)."""
        cfg = self.config
        enriched, ticker, candidate = (
            data["enriched"], data["ticker"], data["range"])
        decisions = []
        # 1. range break (current price vs buffer) — kill-class gate.
        decisions.append(range_break_kill(
            candidate.lower, candidate.upper, ticker.price,
            cfg.range_break_buffer_pct))
        # 2. 15m candle-close lower-boundary kill (closed candle only).
        last_row = latest_valid_row(enriched)
        decisions.append(lower_boundary_15m_kill(
            last_row["close"], candidate.lower, cfg.stop_if_below_lower_pct))
        # 3. equity drawdown kill (2%) vs the persisted high-water reference.
        equity, reference, ref_gate = self._equity_reference(ticker.price)
        decisions.append(ref_gate)
        if reference is not None and equity is not None and reference > 0:
            drawdown = (reference - equity) / reference
            decisions.append(equity_dd_kill(
                drawdown, cfg.max_equity_drawdown_pct))
        # 4. market filter (ADX / ATR / BB width / volume spike).
        decisions.append(market_gate(last_row, cfg.market_filter))
        # 5. open-order capacity.
        decisions.append(open_orders_gate(
            remote_open, cfg.max_open_orders))
        # 6. grid plan (min net profit per cell after quantization + filters).
        if plan_valid is not None:
            decisions.append(_plan_gate(plan_valid))
        return combine(*decisions), equity, reference

    def _equity_reference(self, ticker_price: Decimal):
        """Read equity, apply the reference gate, raise the persisted HWM.

        The reference is raise-only (never lowered, never silently reset).
        A present-but-corrupt reference blocks (fail closed).
        """
        account = self.read_client.account()
        balances = {b.asset: b for b in account.balances}
        quote = balances.get(self._rules.quote_asset)
        base = balances.get(self._rules.base_asset)
        if quote is None:
            return None, None, combine(_fail("ACCOUNT_DATA_UNAVAILABLE"))
        equity = quote.free + quote.locked
        if base is not None:
            equity += (base.free + base.locked) * ticker_price
        raw = self.ledger.get_state("reference_equity")
        reference = _decimal_or_none(raw) if raw is not None else None
        gate = equity_reference_gate(raw, reference)
        if gate.allowed and reference is None:
            # bootstrap on the first valid observation (persisted)
            self.ledger.set_state("reference_equity", str(equity))
            reference = equity
        elif gate.allowed and reference is not None and equity > reference:
            # high-water mark: raise only
            self.ledger.set_state("reference_equity", str(equity))
            reference = equity
        return equity, reference, gate

    # -- reconciliation ----------------------------------------------------------
    def _apply_authoritative_status(self, order_row: dict, status: str,
                                    exchange_order_id, executed_qty: Decimal,
                                    detail: str = "") -> str:
        """Map an AUTHORITATIVE exchange status to the ledger state machine.

        Never invents a transition: only authoritative evidence moves an
        order out of UNKNOWN/PENDING_RECONCILIATION.
        """
        if status == "NEW":
            new_state = ORDER_OPEN
        elif status == "PARTIALLY_FILLED":
            new_state = ORDER_PARTIALLY_FILLED
        elif status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
            new_state = {"FILLED": ORDER_FILLED, "CANCELED": ORDER_CANCELED,
                         "EXPIRED": ORDER_CANCELED, "REJECTED": ORDER_REJECTED}[status]
        else:
            new_state = ORDER_PENDING_RECONCILIATION
        self.ledger.update_order(
            order_row["client_order_id"], state=new_state,
            exchange_order_id=exchange_order_id, fill_qty=executed_qty,
            detail=detail or f"authoritative status {status}")
        return new_state

    def resolve_ledger_order(self, order_row: dict) -> dict:
        """Authoritatively resolve one ledger order (never resubmits)."""
        cid = order_row["client_order_id"]
        symbol = order_row["symbol"]
        try:
            payload = self.read_client.get_order(symbol, cid)
        except BinanceTestnetNetworkError as exc:
            self._event(order_row["cycle_id"], "resolve_unknown",
                        client_order_id=cid,
                        detail=f"query failed: {type(exc).__name__}")
            if order_row["state"] != ORDER_PENDING_RECONCILIATION:
                self.ledger.update_order(
                    cid, state=ORDER_PENDING_RECONCILIATION,
                    detail=f"resolve failed: {type(exc).__name__}")
            return {"cid": cid, "state": ORDER_PENDING_RECONCILIATION}
        except BinanceTestnetError as exc:
            self._event(order_row["cycle_id"], "resolve_failed",
                        client_order_id=cid, detail=type(exc).__name__)
            if order_row["state"] != ORDER_PENDING_RECONCILIATION:
                self.ledger.update_order(
                    cid, state=ORDER_PENDING_RECONCILIATION,
                    detail=f"resolve failed: {type(exc).__name__}")
            return {"cid": cid, "state": ORDER_PENDING_RECONCILIATION}
        status = payload.get("status")
        if status not in _KNOWN_EXCHANGE_STATUSES:
            self.ledger.update_order(
                cid, state=ORDER_PENDING_RECONCILIATION,
                detail=f"unknown exchange status {status!r}")
            return {"cid": cid, "state": ORDER_PENDING_RECONCILIATION}
        state = self._apply_authoritative_status(
            order_row, status, payload.get("orderId"),
            Decimal(str(payload.get("executedQty", "0"))),
            detail="recovered by resolve")
        self._event(order_row["cycle_id"], "resolved",
                    client_order_id=cid, status=status, state=state)
        return {"cid": cid, "state": state, "status": status}

    def reconcile_all(self, cycle_id: int = 0) -> dict:
        """Authoritative snapshot pass over every non-terminal ledger order."""
        outcome, remote, _attempts = self.reconciler.fetch_open_orders(
            self.config.symbol)
        if outcome is not Outcome.CONFIRMED:
            self._event(cycle_id, "reconcile_snapshot_unavailable",
                        outcome=outcome.value)
            # Fail closed: local state untouched; per-order resolution below
            # is skipped because even the snapshot is untrustworthy.
            unresolved = []
            for row in self.ledger.orders_in_states(*NON_TERMINAL_STATES):
                resolved = self.resolve_ledger_order(row)
                unresolved.append(resolved)
            return {"snapshot": outcome.value, "corrected": [],
                    "unresolved": unresolved}
        prefix = f"AGTC-{self.config.symbol}-"
        remote_by_cid = {o.client_order_id: o for o in remote
                         if o.client_order_id.startswith(prefix)}
        foreign = [o.client_order_id for o in remote
                   if not o.client_order_id.startswith(prefix)]
        corrected = []
        for row in self.ledger.orders_in_states(*NON_TERMINAL_STATES):
            cid = row["client_order_id"]
            remote_order = remote_by_cid.get(cid)
            if remote_order is None:
                # Not in the open snapshot: could be filled/canceled/archived
                # or unknown — settle ONLY via the authoritative single-order
                # query (§3: absence is never proof of non-existence).
                corrected.append(self.resolve_ledger_order(row))
                continue
            expected_remote = ("NEW" if row["state"] == ORDER_OPEN
                               else row["state"])
            if remote_order.status == expected_remote:
                # authoritative agreement; refresh fill qty
                self.ledger.update_order(cid, fill_qty=remote_order.executed_qty)
                continue
            corrected.append(self.resolve_ledger_order(row))
        # An exchange-side order with our prefix that we never recorded is
        # an anomaly: record it, never invent local exposure.
        for cid in remote_by_cid:
            if self.ledger.get_order(cid) is None:
                self._event(cycle_id, "unknown_own_order_on_exchange",
                            client_order_id=cid, detail="not in ledger")
        return {"snapshot": "CONFIRMED", "corrected": corrected,
                "unresolved": [c for c in corrected
                               if c["state"] == ORDER_PENDING_RECONCILIATION],
                "foreign_open_orders": foreign}

    # -- kill path ---------------------------------------------------------------
    def activate_kill(self, reason: str, cycle_id: int = 0) -> None:
        """Latch first (persisted), then fail-closed cancel-on-kill."""
        self.ledger.latch_kill(reason, actor="testnet_cycle")
        self._event(cycle_id, "kill_latched", reason=reason)
        for row in self.ledger.orders_in_states(
                ORDER_OPEN, ORDER_PARTIALLY_FILLED,
                ORDER_SUBMITTED_UNKNOWN, ORDER_PENDING_RECONCILIATION):
            cid = row["client_order_id"]
            record = self.reconciler.cancel(row["symbol"], cid, verify=True)
            if record.verdict is not CancelVerdict.CONFIRMED_CANCELED:
                # Not proven: keep it visible, settle via resolve.
                self.resolve_ledger_order(
                    self.ledger.get_order(cid) or row)
                self._event(cycle_id, "kill_cancel_unconfirmed",
                            client_order_id=cid, detail=record.detail)
            else:
                self._event(cycle_id, "kill_cancel_confirmed",
                            client_order_id=cid)

    # -- cycle --------------------------------------------------------------------
    def run_cycle(self, cycle_id: int, *, place_orders: bool) -> dict:
        if self._rules is None:
            # Preflight parses the symbol rules; running without it would
            # skip fail-closed precondition checks entirely.
            raise TestnetCycleSafetyError(
                "preflight() must run before any cycle (fail closed)")
        report: dict[str, Any] = {"cycle": cycle_id, "placed": 0,
                                  "vetoes": [], "orders": []}
        kill = self.ledger.kill_active()
        if kill is not None:
            report["blocked"] = f"KILL_ACTIVE:{kill['reason']}"
            self._event(cycle_id, "cycle_blocked", reason=report["blocked"])
            return report

        # 1. market data (fail-closed on any data problem).
        try:
            data = self._market_cycle_data()
        except (BinanceTestnetError, ValueError, ArithmeticError,
                SymbolRuleError) as exc:
            report["blocked"] = f"MARKET_DATA_FAILED:{type(exc).__name__}"
            self._event(cycle_id, "cycle_blocked", reason=report["blocked"])
            return report
        candidate = data["range"]
        report["range"] = {"lower": str(candidate.lower),
                           "upper": str(candidate.upper),
                           "approved": candidate.approved,
                           "reason": candidate.reason,
                           "quality": candidate.quality}

        # 2. authoritative open-order snapshot.
        outcome, remote, _ = self.reconciler.fetch_open_orders(
            self.config.symbol)
        if outcome is not Outcome.CONFIRMED:
            report["blocked"] = f"OPEN_ORDERS_UNAVAILABLE:{outcome.value}"
            self._event(cycle_id, "cycle_blocked", reason=report["blocked"])
            return report
        prefix = f"AGTC-{self.config.symbol}-"
        remote_own = [o for o in remote if o.client_order_id.startswith(prefix)]
        foreign = [o.client_order_id for o in remote
                   if not o.client_order_id.startswith(prefix)]
        if foreign:
            # Fail closed: never place orders alongside foreign exposure.
            report["blocked"] = f"FOREIGN_OPEN_ORDERS:{foreign}"
            self._event(cycle_id, "cycle_blocked", reason=report["blocked"])
            return report

        # 3. recovery of any non-terminal ledger orders from prior runs.
        recovered = self.reconcile_all(cycle_id)
        if any(c["state"] == ORDER_PENDING_RECONCILIATION
               for c in recovered.get("corrected", [])) or \
                recovered.get("unresolved"):
            report["blocked"] = "UNRESOLVED_ORDERS_PENDING_RECONCILIATION"
            report["unresolved"] = recovered.get("unresolved", [])
            self._event(cycle_id, "cycle_blocked", reason=report["blocked"])
            return report

        # 4. grid plan (production math: geometric grid + quantized plan).
        rules = self._rules
        if not candidate.approved:
            report["vetoes"].append(f"RANGE:{candidate.reason}")
            self._event(cycle_id, "veto", gate="RANGE", reason=candidate.reason)
            return report
        try:
            levels, _effective_upper = build_geometric_grid(
                candidate.lower, candidate.upper, self.config.grid_step_pct,
                min_cells=self.config.min_cells,
                max_levels=self.config.max_levels)
            plan = validate_quantized_order_plan(
                levels, rules, self.config.order_quote_size,
                data["ticker"].price, self.config.maker_fee,
                self.config.maker_fee, self.config.slippage_roundtrip_pct,
                self.config.hard_min_net_pct, self.config.max_open_orders)
        except (ValueError, SymbolRuleError, ArithmeticError) as exc:
            report["vetoes"].append(f"GRID:{exc}")
            self._event(cycle_id, "veto", gate="GRID", reason=str(exc))
            return report
        if not plan.allowed:
            report["vetoes"].append(f"GRID_PLAN:{plan.reason}")
            self._event(cycle_id, "veto", gate="GRID_PLAN",
                        reason=plan.reason)
            # continue: still run the risk stack for the record?  No — the
            # plan gate already vetoed; record and stop this cycle.
            return report

        # 5. full risk stack (authoritative veto).
        decision, equity, reference = self._risk_decision(
            data, plan, len(remote_own))
        report["risk_decision"] = decision.reason
        report["equity"] = str(equity) if equity is not None else None
        report["reference_equity"] = str(reference) if reference is not None else None
        self._event(cycle_id, "risk_decision", reason=decision.reason,
                    equity=str(equity) if equity is not None else None)
        if not decision.allowed:
            report["vetoes"].append(f"RISK:{decision.reason}")
            if "EQUITY_DRAWDOWN_KILL" in decision.reasons or \
                    any(r.startswith(("RANGE_BREAK", "LOWER_BOUNDARY_STOP"))
                        for r in decision.reasons):
                # Kill-class veto: latch (persist FIRST), cancel, stop.
                self.activate_kill(decision.reason, cycle_id)
                report["killed"] = True
            return report

        # 6. order intents: lowest allowed BUY cells, strictly inside range.
        intents = []
        for cell in plan.cells:
            if not cell.allowed or len(intents) >= self.config.max_orders_per_cycle:
                continue
            price_gate = strict_order_price_gate(
                candidate.lower, candidate.upper, cell.buy_price)
            if not price_gate.allowed:
                self._event(cycle_id, "veto", gate="ORDER_PRICE",
                            reason=price_gate.reason,
                            price=str(cell.buy_price))
                continue
            intents.append(cell)
        if not intents:
            report["vetoes"].append("NO_VALID_CELLS")
            return report

        # 7. balance gate (fail-closed before any submission).
        required = sum(
            (cell.buy_price * cell.quantity for cell in intents),
            Decimal("0")) * self.config.balance_buffer
        account = self.read_client.account()
        balances = {b.asset: b for b in account.balances}
        quote = balances.get(rules.quote_asset)
        if quote is None or quote.free < required:
            report["vetoes"].append(
                f"INSUFFICIENT_TESTNET_QUOTE:required={required},"
                f"free={quote.free if quote else 0}")
            self._event(cycle_id, "veto", gate="BALANCE",
                        required=str(required),
                        free=str(quote.free) if quote else "0")
            return report

        # 8. placement (bounded, deterministic cids, never resubmitted).
        # Without a write-capable client the runner can NEVER submit —
        # intents are recorded for observability and the cycle stays
        # read-only (rehearsal semantics).
        intents_payload = [
            {"price": str(c.buy_price), "quantity": str(c.quantity)}
            for c in intents]
        if self.order_client is None or not place_orders:
            report["intents_only"] = intents_payload
            self._event(cycle_id, "rehearsal_intents", count=len(intents))
            return report
        for seq, cell in enumerate(intents, start=1):
            cid = self._cid(cycle_id, seq)
            if not self.ledger.insert_order(
                    client_order_id=cid, run_id=self.run_id or 0,
                    cycle_id=cycle_id, symbol=self.config.symbol, side="BUY",
                    price=cell.buy_price, quantity=cell.quantity,
                    state=ORDER_INTENT):
                # Deterministic ledger duplicate: never reuse a cid.
                self._event(cycle_id, "duplicate_cid_refused",
                            client_order_id=cid)
                continue
            self._submit(cid, cycle_id, cell)
            report["placed"] += 1
            report["orders"].append(cid)

        # 9. cycle-end verified cancellation of still-open orders.
        self._cancel_open_orders(cycle_id)
        return report

    def _submit(self, cid: str, cycle_id: int, cell) -> None:
        """One placement attempt.  Ack / deterministic rejection / UNKNOWN —
        the UNKNOWN path resolves by clientOrderId and NEVER resubmits."""
        try:
            ack = self.order_client.place_limit_maker_order(
                self.config.symbol, "BUY", cell.quantity, cell.buy_price, cid)
        except BinanceTestnetOrderRejectedError as exc:
            self.ledger.update_order(
                cid, state=ORDER_REJECTED, detail=f"code={exc.code}")
            self._event(cycle_id, "order_rejected", client_order_id=cid,
                        code=exc.code)
            return
        except BinanceTestnetError as exc:
            # UNKNOWN: resolve deterministically; never resubmit (§4).
            self.ledger.update_order(
                cid, state=ORDER_SUBMITTED_UNKNOWN,
                detail=f"submit outcome unknown: {type(exc).__name__}")
            self._event(cycle_id, "submit_unknown", client_order_id=cid,
                        error=type(exc).__name__)
            row = self.ledger.get_order(cid)
            self.resolve_ledger_order(row)
            return
        state = self._apply_authoritative_status(
            self.ledger.get_order(cid), ack.status, ack.order_id,
            ack.executed_qty, detail="placement ack")
        self._event(cycle_id, "order_placed", client_order_id=cid,
                    exchange_order_id=ack.order_id, status=ack.status,
                    state=state)

    def _cancel_open_orders(self, cycle_id: int) -> list[str]:
        """Verified cancellation of still-open own orders (§5)."""
        canceled = []
        for row in self.ledger.orders_in_states(
                ORDER_OPEN, ORDER_PARTIALLY_FILLED):
            cid = row["client_order_id"]
            record = self.reconciler.cancel(row["symbol"], cid, verify=True)
            if record.verdict is CancelVerdict.CONFIRMED_CANCELED:
                self.ledger.update_order(
                    cid, state=ORDER_CANCELED, detail=record.detail)
                canceled.append(cid)
                self._event(cycle_id, "order_canceled", client_order_id=cid)
            else:
                # Unproven cancel: settle via authoritative resolve; an
                # unfilled race leaves PENDING_RECONCILIATION visible.
                self.resolve_ledger_order(self.ledger.get_order(cid) or row)
                self._event(cycle_id, "cancel_unconfirmed",
                            client_order_id=cid, detail=record.detail)
        return canceled

    # -- run loop -------------------------------------------------------------------
    def run(self, *, place_orders: bool, max_cycles: Optional[int] = None,
            config_json: str = "{}") -> dict:
        """Bounded run: preflight → cycles → cleanup → proof."""
        self.run_id = self.ledger.start_run(config_json)
        summary: dict[str, Any] = {
            "run_id": self.run_id, "cycles": [], "cleanup": None,
            "stopped_reason": None,
        }
        try:
            self.preflight()
        except TestnetCycleSafetyError as exc:
            summary["stopped_reason"] = f"PREFLIGHT:{exc}"
            self._event(0, "preflight_failed", detail=str(exc))
            summary["cleanup"] = self.cleanup()
            self.ledger.finish_run(self.run_id, "ABORTED",
                                   summary["stopped_reason"])
            return summary
        kill = self.ledger.kill_active()
        if kill is not None:
            # Kill branch: recovery + best-effort cleanup ONLY, no new orders.
            self._event(0, "kill_branch_entered", reason=kill["reason"])
            self.reconcile_all(0)
            summary["cleanup"] = self.cleanup()
            summary["stopped_reason"] = f"KILL_ACTIVE:{kill['reason']}"
            self.ledger.finish_run(self.run_id, "KILLED", kill["reason"])
            return summary

        total = int(max_cycles or self.config.max_cycles)
        for cycle_id in range(1, total + 1):
            if run_loop_boundary_check(self.shutdown):
                summary["stopped_reason"] = (
                    f"GRACEFUL_SHUTDOWN:{self.shutdown.describe()}")
                self._event(cycle_id, "graceful_shutdown",
                            detail=self.shutdown.describe())
                break
            try:
                report = self.run_cycle(cycle_id, place_orders=place_orders)
                summary["cycles"].append(report)
            except TestnetCycleSafetyError as exc:
                summary["stopped_reason"] = f"SAFETY:{exc}"
                self._event(cycle_id, "safety_stop", detail=str(exc))
                break
            except BinanceTestnetError as exc:
                # Network interruption mid-cycle: fail closed, state is
                # persisted; the next run recovers from the ledger.
                summary["stopped_reason"] = f"NETWORK:{type(exc).__name__}"
                self._event(cycle_id, "network_interruption",
                            error=type(exc).__name__)
                break
            if report.get("killed"):
                # Kill latched mid-run: no further cycles (recovery/cleanup
                # only); the latch persists for the next run's kill branch.
                summary["stopped_reason"] = "KILL_ACTIVATED_MID_RUN"
                break
            if cycle_id < total:
                self.sleep(self.config.poll_interval_s)
        self.shutdown.complete()
        summary["cleanup"] = self.cleanup()
        status = "COMPLETED" if summary["stopped_reason"] is None else "STOPPED"
        self.ledger.finish_run(self.run_id, status,
                               summary["stopped_reason"] or "")
        return summary

    # -- cleanup / proof -----------------------------------------------------------
    def cleanup(self) -> dict:
        """Reconcile → cancel confirmed-open own orders → re-resolve →
        PROVE zero own open orders and zero non-terminal ledger orders.

        Foreign orders are never touched and are reported.  Anything
        unprovable fails with exact client order ids.
        """
        reconciled = self.reconcile_all(0)
        canceled = []
        for row in self.ledger.orders_in_states(
                ORDER_OPEN, ORDER_PARTIALLY_FILLED):
            cid = row["client_order_id"]
            record = self.reconciler.cancel(row["symbol"], cid, verify=True)
            if record.verdict is CancelVerdict.CONFIRMED_CANCELED:
                self.ledger.update_order(
                    cid, state=ORDER_CANCELED, detail=record.detail)
                canceled.append(cid)
            else:
                self.resolve_ledger_order(self.ledger.get_order(cid) or row)
        # Final proof: authoritative snapshot must show zero own open orders.
        outcome, remote, _ = self.reconciler.fetch_open_orders(
            self.config.symbol)
        prefix = f"AGTC-{self.config.symbol}-"
        if outcome is not Outcome.CONFIRMED:
            return {"ok": False,
                    "reason": f"CLEANUP_PROOF_UNAVAILABLE:{outcome.value}",
                    "canceled": canceled, "unresolved": [], "foreign": []}
        own_open = [o.client_order_id for o in remote
                    if o.client_order_id.startswith(prefix)]
        foreign = [o.client_order_id for o in remote
                   if not o.client_order_id.startswith(prefix)]
        non_terminal = [o["client_order_id"] for o in
                        self.ledger.orders_in_states(*NON_TERMINAL_STATES)]
        unresolved = sorted(set(own_open) | set(non_terminal))
        return {
            "ok": not unresolved,
            "reason": None if not unresolved else
            f"UNRESOLVED_ORDERS:{unresolved}",
            "canceled": canceled,
            "unresolved": unresolved,
            "foreign": foreign,
        }

    # -- observability ----------------------------------------------------------------
    def status(self) -> dict:
        orders = self.ledger.all_orders()
        return {
            "symbol": self.config.symbol,
            "run_id": self.run_id,
            "kill_active": self.ledger.kill_active(),
            "reference_equity": self.ledger.get_state("reference_equity"),
            "orders": orders,
            "non_terminal": [o["client_order_id"] for o in orders
                             if not is_terminal(o["state"])],
            "events": self.ledger.events(self.run_id),
        }


def _fail(reason: str):
    """One-decision helper for the combined risk stack."""
    from risk_engine import RiskDecision
    return RiskDecision(False, (reason,))


def _plan_gate(plan_valid):
    """RiskDecision view of the quantized grid-plan validation."""
    from risk_engine import RiskDecision
    return RiskDecision(bool(plan_valid.allowed),
                        () if plan_valid.allowed else (plan_valid.reason,))
