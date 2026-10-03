"""Gated Testnet Execution Bridge — real order mirroring for the runtime.

Roadmap B (modular order execution) for the multi-symbol runtime: the
risk-gated paper cycle remains the decision engine, and this bridge mirrors
its outcomes to REAL orders on the Binance Spot TESTNET:

    paper cycle (risk-vetted intents)
        → mirror: real LIMIT_MAKER order, same clientOrderId/price/quantity
        → reconcile: authoritative per-cid status each cycle
        → cancel propagation: strategy auto-exit / kill cancel passes

Safety architecture
-------------------
* **Testnet only.**  The write-capable client re-asserts the full adapter
  barrier (testnet base URL, ``dry_run=true``, ``allow_live_execution=false``);
  there is no live path anywhere in this module.
* **Double gate.**  Mirroring is enabled only when BOTH
  ``execution.testnet_execution: true`` in config.yaml AND
  ``TESTNET_ORDERS_ENABLED=true`` in the environment.  Either alone (or any
  other value) leaves the bridge disabled: every method is a recorded no-op.
* **Crash-safe placement.**  A ``PENDING_PLACE`` row is persisted BEFORE the
  POST; the POST is never retried (the exchange rejects a duplicate
  clientOrderId, verified −2010).  A lost/ambiguous ack lands in the
  ``UNKNOWN`` state and is settled only by an authoritative re-query.
* **Paper ledger untouched.**  Real fills are tracked in the bridge's own
  ``execution_orders`` ledger.  The paper engine keeps its deterministic
  fills, so there is exactly one writer per ledger and no double accounting.
  Divergence between the two is reported, never silently reconciled.
* **Fail-closed on unknown remote orders.**  Own-namespace orders resting on
  the exchange that the runtime did not submit block new mirroring until an
  operator resolves them.

Limitations (deliberate, documented in LIMITATIONS.md):
* LIMIT_MAKER only (post-only) — the verified write surface.
* The bridge cancels real orders on exit/kill but never market-sells real
  inventory; real base holdings on testnet are reported for operator action.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from binance_testnet import BinanceTestnetClient, BinanceTestnetError
from order_engine import PaperOrder
from testnet_orders import (
    BinanceTestnetOrderClient,
    BinanceTestnetOrderError,
    BinanceTestnetOrderRejectedError,
    ORDERS_ENABLED_ENV,
)

logger = logging.getLogger("adaptive_grid.execution_bridge")

#: Own clientOrderId namespace (grid identity from order_engine.make_client_order_id).
_OWN_CID_PREFIXES = ("AG",)

#: Ledger states (explicit state machine; anything else is a bug).
STATE_PENDING_PLACE = "PENDING_PLACE"  # persisted before the POST
STATE_OPEN = "OPEN"                    # confirmed resting on the exchange
STATE_FILLED = "FILLED"                # confirmed fully filled
STATE_CANCELED = "CANCELED"            # confirmed canceled
STATE_EXPIRED = "EXPIRED"              # post-only rejected by crossing (EXPIRED)
STATE_REJECTED = "REJECTED"            # deterministically rejected, nothing rested
STATE_UNKNOWN = "UNKNOWN"              # ambiguous outcome; reconcile-only

_TERMINAL_STATES = frozenset({STATE_FILLED, STATE_CANCELED, STATE_EXPIRED,
                               STATE_REJECTED})
_OPEN_STATES = frozenset({STATE_PENDING_PLACE, STATE_OPEN, STATE_UNKNOWN})

_ACK_STATUS_MAP = {
    "NEW": STATE_OPEN,
    "PARTIALLY_FILLED": STATE_OPEN,
    "FILLED": STATE_FILLED,
    "CANCELED": STATE_CANCELED,
    "EXPIRED": STATE_EXPIRED,
    "PENDING_CANCEL": STATE_OPEN,
    "REJECTED": STATE_REJECTED,
}


def resolve_bridge_gate(cfg: dict[str, Any],
                        orders_enabled_env: bool) -> "BridgeGate":
    """Resolve the double gate (config AND env).  Fail-closed both ways.

    ``orders_enabled_env`` is the strict ``TESTNET_ORDERS_ENABLED`` value;
    it must already be validated by the caller (None/invalid is disabled).
    """
    reasons = []
    execution_cfg = cfg.get("execution", {}) if isinstance(cfg, dict) else {}
    config_flag = execution_cfg.get("testnet_execution", False)
    if config_flag is not True:
        reasons.append(
            f"CONFIG_DISABLED (execution.testnet_execution={config_flag!r})")
    if orders_enabled_env is not True:
        reasons.append(f"ENV_GATE_DISABLED ({ORDERS_ENABLED_ENV}!=true)")
    if reasons:
        return BridgeGate(False, tuple(reasons))
    return BridgeGate(True, ())


@dataclass(frozen=True)
class BridgeGate:
    """Resolved bridge gate with explicit, loggable reasons."""
    enabled: bool
    reasons: tuple[str, ...]


class TestnetExecutionBridge:
    """Mirrors risk-gated paper orders to real Binance Spot TESTNET orders.

    The paper ledger is never written by this class.  Every real order is
    tracked in the per-symbol ``execution_orders`` table with an explicit
    state machine and deterministic clientOrderId identity, so a restart or
    a repeat cycle can never duplicate a placement.
    """

    # Not a pytest test class despite the Test* name.
    __test__ = False

    def __init__(
        self,
        order_client: BinanceTestnetOrderClient,
        market_client: BinanceTestnetClient,
        ledger: "ExecutionLedger",
        symbol: str,
    ) -> None:
        self._orders = order_client
        self._market = market_client
        self.ledger = ledger
        self.symbol = str(symbol).upper()

    # -- mirroring -----------------------------------------------------------

    def mirror_order(self, intent) -> dict[str, Any]:
        """Place one real LIMIT_MAKER order for a risk-gated paper intent.

        Idempotent: a clientOrderId already known to the ledger is never
        re-placed (the exchange enforces the same rule with −2010).  Any
        placement error is recorded and reported — never raised into the
        paper cycle path.
        """
        cid = str(intent.client_order_id)
        existing = self.ledger.get(cid)
        if existing is not None:
            return {"mirrored": False, "cid": cid,
                    "state": existing["state"],
                    "reason": "ALREADY_IN_LEDGER"}
        self.ledger.record_pending(cid, self.symbol, intent.side,
                                   intent.price, intent.quantity)
        try:
            ack = self._orders.place_limit_maker_order(
                self.symbol, intent.side, intent.quantity, intent.price, cid)
        except BinanceTestnetOrderRejectedError as exc:
            # Deterministic rejection: the order does not exist remotely.
            self.ledger.record_state(cid, STATE_REJECTED, note=str(exc))
            return {"mirrored": False, "cid": cid, "state": STATE_REJECTED,
                    "reason": "REJECTED"}
        except BinanceTestnetOrderError as exc:
            # UNKNOWN: never retried; settled by authoritative reconciliation.
            self.ledger.record_state(cid, STATE_UNKNOWN, note=str(exc))
            return {"mirrored": False, "cid": cid, "state": STATE_UNKNOWN,
                    "reason": "UNKNOWN_PLACE_OUTCOME"}
        except Exception as exc:  # §16 surprise-exception guard
            self.ledger.record_state(cid, STATE_UNKNOWN,
                                     note=f"unexpected:{type(exc).__name__}")
            return {"mirrored": False, "cid": cid, "state": STATE_UNKNOWN,
                    "reason": "UNEXPECTED_PLACE_ERROR"}
        state = _ACK_STATUS_MAP.get(ack.status, STATE_UNKNOWN)
        self.ledger.record_ack(cid, state, ack.order_id, ack.executed_qty)
        return {"mirrored": True, "cid": cid, "state": state,
                "exchange_order_id": ack.order_id}

    # -- cancel propagation ---------------------------------------------------

    def cancel_all_open(self, actor: str) -> dict[str, Any]:
        """Cancel every non-terminal mirrored order on the exchange.

        Used by the strategy auto-exit and kill paths after the local cancel
        pass.  Ambiguous cancels (-2011/-2013) are settled ONLY by an
        authoritative re-query; anything else stays UNKNOWN for the next
        reconciliation pass.
        """
        results = []
        for row in self.ledger.orders_in_states(*_OPEN_STATES):
            cid = row["client_order_id"]
            if row["state"] == STATE_PENDING_PLACE:
                # No confirmed placement: never cancel blind. Reconciliation
                # settles whether the order exists.
                results.append({"cid": cid, "state": row["state"],
                                "canceled": False, "reason": "PENDING_PLACE"})
                continue
            results.append(self._cancel_one(cid, actor))
        return {"symbol": self.symbol, "cancels": results}

    def _cancel_one(self, cid: str, actor: str) -> dict[str, Any]:
        try:
            ack = self._orders.cancel_order_by_client_id(self.symbol, cid)
        except BinanceTestnetOrderRejectedError as exc:
            if getattr(exc, "ambiguous", False):
                # Ambiguous: the order may have filled before the cancel
                # arrived.  Settle by authoritative re-query only.
                settled = self._resolve_cid(cid)
                if settled is not None:
                    return {"cid": cid, "canceled": True,
                            "state": settled["state"],
                            "reason": "AMBIGUOUS_RESOLVED_BY_REQUERY"}
                return {"cid": cid, "canceled": False, "state": STATE_UNKNOWN,
                        "reason": f"AMBIGUOUS_UNRESOLVED: {exc}"}
            return {"cid": cid, "canceled": False, "state": None,
                    "reason": f"REJECT: {exc}"}
        except BinanceTestnetOrderError as exc:
            return {"cid": cid, "canceled": False, "state": None,
                    "reason": f"ERROR: {exc}"}
        except Exception as exc:  # §16 surprise-exception guard
            return {"cid": cid, "canceled": False, "state": None,
                    "reason": f"UNEXPECTED: {type(exc).__name__}: {exc}"}
        self.ledger.record_ack(cid, STATE_CANCELED, ack.order_id,
                               ack.executed_qty)
        return {"cid": cid, "canceled": True, "state": STATE_CANCELED,
                "reason": f"CANCELED ({actor})"}

    # -- reconciliation ---------------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        """Authoritative status pass over every non-terminal mirrored order.

        Each pending/open/unknown cid is re-queried by clientOrderId
        (read-only).  Confirmed terminal states are recorded; a missing
        order stays UNKNOWN (missing is ambiguous on Binance Spot — it may
        have filled and archived).  Returns a report for the risk-event
        log; never raises on per-order errors.
        """
        rows = self.ledger.orders_in_states(*_OPEN_STATES)
        report = {"symbol": self.symbol, "checked": len(rows),
                  "resolved": [], "unknown": [], "errors": []}
        for row in rows:
            cid = row["client_order_id"]
            settled = self._resolve_cid(cid)
            if settled is None:
                if row["state"] == STATE_PENDING_PLACE:
                    report["unknown"].append(
                        {"cid": cid, "reason": "PLACE_ACK_LOST"})
                else:
                    report["unknown"].append(
                        {"cid": cid, "reason": "QUERY_UNAVAILABLE"})
                continue
            report["resolved"].append({"cid": cid, **settled})
        return report

    def _resolve_cid(self, cid: str) -> Optional[dict[str, Any]]:
        """Authoritatively settle one cid; None when unresolvable this pass."""
        try:
            payload = self._market.get_order(self.symbol, cid)
        except BinanceTestnetError as exc:
            logger.debug("reconcile query failed for %s: %s", cid,
                         type(exc).__name__)
            return None
        except Exception:
            return None
        status = str(payload.get("status", "")).upper()
        state = _ACK_STATUS_MAP.get(status)
        if state is None:
            return None
        executed = _decimal_or_zero(payload.get("executedQty"))
        exchange_order_id = payload.get("orderId")
        if state in _TERMINAL_STATES:
            self.ledger.record_ack(cid, state, exchange_order_id, executed)
        else:
            self.ledger.record_progress(cid, executed, exchange_order_id)
        return {"state": state, "executed_qty": str(executed),
                "exchange_order_id": exchange_order_id}

    def unknown_remote_orders(self) -> list[dict[str, Any]]:
        """Orders resting on the exchange that this runtime did not place.

        Fail-closed signal: any remote open order whose clientOrderId is not
        in the ledger — an own-namespace orphan OR a foreign order — blocks
        new mirroring until an operator resolves it (the established
        harness policy: foreign presence refuses placement).
        """
        try:
            remote = self._market.open_orders(self.symbol)
        except BinanceTestnetError:
            return []  # query unavailable: reconcile() reports separately
        except Exception:
            return []
        known = {row["client_order_id"] for row in self.ledger.all()}
        unknown = []
        for snap in remote:
            cid = str(getattr(snap, "client_order_id", "") or "")
            if cid and cid not in known:
                unknown.append({"cid": cid, "side": getattr(snap, "side", ""),
                                "price": str(getattr(snap, "price", "")),
                                "quantity": str(getattr(snap, "orig_qty", ""))})
        return unknown

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        open_rows = self.ledger.orders_in_states(*_OPEN_STATES)
        return {
            "symbol": self.symbol,
            "open_mirrored": len(open_rows),
            "total_mirrored": len(self.ledger.all()),
        }


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


class ExecutionLedger:
    """Minimal, crash-safe per-symbol ledger for mirrored real orders.

    Lives in the per-symbol SQLite database (own table, own state machine);
    no existing table is migrated or touched.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS execution_orders (
            client_order_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price TEXT NOT NULL,
            quantity TEXT NOT NULL,
            state TEXT NOT NULL,
            exchange_order_id INTEGER,
            executed_qty TEXT NOT NULL DEFAULT '0',
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        con = sqlite3.connect(db_path)
        try:
            con.execute(self._DDL)
            con.commit()
        finally:
            con.close()

    def _connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_pending(self, cid: str, symbol: str, side: str,
                       price: Decimal, quantity: Decimal) -> None:
        now = self._now()
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO execution_orders(client_order_id,"
                "symbol,side,price,quantity,state,executed_qty,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, str(symbol).upper(), str(side).upper(), str(price),
                 str(quantity), STATE_PENDING_PLACE, "0", now, now))
            con.commit()
        finally:
            con.close()

    def record_state(self, cid: str, state: str, note: str = "") -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE execution_orders SET state=?, note=?, updated_at=? "
                "WHERE client_order_id=?",
                (state, note or None, self._now(), cid))
            con.commit()
        finally:
            con.close()

    def record_ack(self, cid: str, state: str, exchange_order_id: Any,
                   executed_qty: Decimal) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE execution_orders SET state=?, exchange_order_id=?, "
                "executed_qty=?, updated_at=? WHERE client_order_id=?",
                (state, exchange_order_id, str(executed_qty), self._now(),
                 cid))
            con.commit()
        finally:
            con.close()

    def record_progress(self, cid: str, executed_qty: Decimal,
                        exchange_order_id: Any) -> None:
        """Partial-fill progress on a still-open order (state unchanged)."""
        con = self._connect()
        try:
            con.execute(
                "UPDATE execution_orders SET executed_qty=?, "
                "exchange_order_id=COALESCE(?, exchange_order_id), "
                "updated_at=? WHERE client_order_id=?",
                (str(executed_qty), exchange_order_id, self._now(), cid))
            con.commit()
        finally:
            con.close()

    def get(self, cid: str) -> Optional[dict[str, Any]]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM execution_orders WHERE client_order_id=?",
                (cid,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def orders_in_states(self, *states: str) -> list[dict[str, Any]]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT * FROM execution_orders WHERE state IN "
                f"({placeholders}) ORDER BY created_at",
                tuple(states)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def all(self) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM execution_orders ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def build_bridge_from_env(
    cfg: dict[str, Any],
    symbol: str,
    db_path: str,
) -> tuple[Optional["TestnetExecutionBridge"], BridgeGate]:
    """Build the bridge when (and only when) the double gate is open.

    Returns ``(None, gate)`` with explicit reasons when disabled.  Any
    construction failure is fail-closed: the bridge stays disabled and the
    reason is recorded on the gate.
    """
    from testnet_orders import load_testnet_orders_enabled_from_env
    try:
        orders_enabled = load_testnet_orders_enabled_from_env()
    except Exception as exc:
        return None, BridgeGate(False, (f"ENV_GATE_INVALID: {exc}",))
    gate = resolve_bridge_gate(cfg, orders_enabled)
    if not gate.enabled:
        return None, gate
    try:
        from binance_testnet import load_testnet_config_from_env
        config = load_testnet_config_from_env()
        order_client = BinanceTestnetOrderClient(config, orders_enabled=True)
        market_client = BinanceTestnetClient(config)
        ledger = ExecutionLedger(db_path)
    except Exception as exc:
        return None, BridgeGate(False, (f"BRIDGE_CONSTRUCTION_FAILED: "
                                        f"{type(exc).__name__}",))
    return (TestnetExecutionBridge(order_client, market_client, ledger,
                                   symbol), gate)


__all__ = [
    "BridgeGate",
    "ExecutionLedger",
    "TestnetExecutionBridge",
    "build_bridge_from_env",
    "resolve_bridge_gate",
    "STATE_PENDING_PLACE",
    "STATE_OPEN",
    "STATE_FILLED",
    "STATE_CANCELED",
    "STATE_EXPIRED",
    "STATE_REJECTED",
    "STATE_UNKNOWN",
]
