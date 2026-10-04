"""Binance Spot access and order execution.

Safety model:
- Spot only. No futures/margin/leverage/shorting anywhere.
- Testnet is the default environment. Live endpoints and live credentials
  are used ONLY when all three Config live gates are satisfied
  (DRY_RUN=false AND ALLOW_LIVE_EXECUTION=true AND BINANCE_ENV=live).
- DRY_RUN never submits anything to Binance: orders are simulated, fills
  are simulated from closed candles, inventory/PnL/fees are tracked in
  the state database.
- Order submissions are never retried blindly: on a network failure the
  order is reconciled by client order id first; an unknown final state
  raises OrderUnknownState (fail-closed — the bot stops the symbol).
- GET market-data requests get bounded retries; signed POST/DELETE
  requests get none.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional

from grid import ExchangeFilters
from state import StateStore

log = logging.getLogger("exchange")

TESTNET_BASE = "https://testnet.binance.vision"
LIVE_BASE = "https://api.binance.com"

_TERMINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}


class ExchangeError(Exception):
    """Request-level failure (HTTP error or exhausted retries)."""


class OrderUnknownState(ExchangeError):
    """The final state of an order submission is unknown (fail-closed)."""


def exchange_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


class BinanceSpot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = LIVE_BASE if cfg.allow_live else TESTNET_BASE
        self._api_key, self._api_secret = cfg.api_credentials
        self._recv_window = 5000

    @property
    def environment(self) -> str:
        return "LIVE" if self.cfg.allow_live else "TESTNET"

    # ----- transport -----

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False,
        retries: int = 0,
    ) -> object:
        params = dict(params or {})
        headers = {"User-Agent": "adaptive-grid/1.0"}
        if signed:
            if self.cfg.dry_run:
                raise ExchangeError("signed request refused: DRY_RUN is enabled")
            if not self._api_key or not self._api_secret:
                raise ExchangeError("signed request refused: no credentials for this environment")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self._recv_window
            query = urllib.parse.urlencode(params)
            signature = hmac.new(
                self._api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MBX-APIKEY"] = self._api_key
            url = f"{self.base_url}{path}?{query}&signature={signature}"
        else:
            url = f"{self.base_url}{path}"
            if params:
                url += "?" + urllib.parse.urlencode(params)

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                raise ExchangeError(f"{method} {path} -> HTTP {exc.code}: {body}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
        raise ExchangeError(f"{method} {path} failed after retries: {last_error}")

    # ----- public market data -----

    def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> List[Dict]:
        raw = self._request(
            "GET",
            "/api/v3/klines",
            {"symbol": exchange_symbol(symbol), "interval": interval, "limit": limit},
            retries=2,
        )
        out = []
        for k in raw:
            out.append(
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                }
            )
        return out

    def get_filters(self, symbol: str) -> ExchangeFilters:
        info = self._request(
            "GET",
            "/api/v3/exchangeInfo",
            {"symbol": exchange_symbol(symbol)},
            retries=2,
        )
        symbols = info.get("symbols") or []
        if not symbols:
            raise ExchangeError(f"unknown symbol on exchange: {symbol}")
        tick = step = min_qty = None
        min_notional: Optional[float] = None
        for f in symbols[0].get("filters", []):
            ftype = f.get("filterType")
            if ftype == "PRICE_FILTER":
                tick = float(f["tickSize"])
            elif ftype == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("notional", 0)) or 0)
        if not tick or not step or min_notional is None:
            raise ExchangeError(f"incomplete exchange filters for {symbol}")
        return ExchangeFilters(tick, step, min_notional, min_qty or 0.0)

    # ----- trading (signed; disabled under DRY_RUN) -----

    def create_limit_maker_order(
        self, symbol: str, side: str, price: float, qty: float, client_order_id: str
    ) -> Dict:
        params = {
            "symbol": exchange_symbol(symbol),
            "side": side,
            "type": "LIMIT_MAKER",
            "quantity": format(qty, ".8f"),
            "price": format(price, ".8f"),
            "newClientOrderId": client_order_id,
        }
        return self._request("POST", "/api/v3/order", params, signed=True)

    def create_market_order(self, symbol: str, side: str, qty: float, client_order_id: str) -> Dict:
        params = {
            "symbol": exchange_symbol(symbol),
            "side": side,
            "type": "MARKET",
            "quantity": format(qty, ".8f"),
            "newClientOrderId": client_order_id,
        }
        return self._request("POST", "/api/v3/order", params, signed=True)

    def cancel_order(self, symbol: str, client_order_id: str) -> Dict:
        return self._request(
            "DELETE",
            "/api/v3/order",
            {"symbol": exchange_symbol(symbol), "origClientOrderId": client_order_id},
            signed=True,
        )

    def get_order(self, symbol: str, client_order_id: str) -> Optional[Dict]:
        """Order status by client id; None when the order does not exist."""
        try:
            return self._request(
                "GET",
                "/api/v3/order",
                {"symbol": exchange_symbol(symbol), "origClientOrderId": client_order_id},
                signed=True,
            )
        except ExchangeError as exc:
            if "-2011" in str(exc):  # Unknown order sent.
                return None
            raise

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        params: Dict[str, object] = {}
        if symbol is not None:
            params["symbol"] = exchange_symbol(symbol)
        return self._request("GET", "/api/v3/openOrders", params, signed=True)

    def get_my_trades(self, symbol: str, order_id: Optional[int] = None) -> List[Dict]:
        params: Dict[str, object] = {"symbol": exchange_symbol(symbol)}
        if order_id is not None:
            params["orderId"] = order_id
        return self._request("GET", "/api/v3/myTrades", params, signed=True)

    def get_account(self) -> Dict:
        """Authoritative account balances (signed; refused under DRY_RUN)."""
        return self._request("GET", "/api/v3/account", signed=True)

    def get_balance(self, asset: str) -> float:
        """Free + locked balance for one asset, from the account snapshot."""
        account = self.get_account()
        for b in account.get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free") or 0) + float(b.get("locked") or 0)
        return 0.0


class Accounting:
    """Thin façade over the authoritative fill recording in StateStore.

    StateStore.record_fill is THE single accounting event per exchange
    trade: atomic (fill row + inventory/avg-cost/realized-PnL in one
    transaction) and idempotent (trade_id is the idempotency key). This
    class only adds logging; executors must route every fill through it
    exactly once via BaseExecutor._account_trades.
    """

    def __init__(self, cfg, store: StateStore):
        self.cfg = cfg
        self.store = store

    def record_trade(
        self,
        symbol: str,
        order_id: int,
        side: str,
        price: float,
        qty: float,
        fee: float,
        trade_id: str,
    ) -> bool:
        recorded = self.store.record_fill(
            order_id, symbol, side, price, qty, fee, trade_id=trade_id
        )
        if recorded:
            log.info(
                "fill %s %s qty=%s price=%s fee=%s trade=%s",
                symbol, side, qty, price, fee, trade_id,
            )
        return recorded


def _new_client_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


# Tolerances for quantity bookkeeping:
QTY_TOLERANCE = 1e-9         # absolute epsilon: "effectively zero" quantity
LIQ_REL_TOLERANCE = 1e-6     # relative tolerance for liquidation/balance checks


class BaseExecutor:
    """Order lifecycle shared by dry-run and live executors.

    Accounting invariant: there is exactly ONE authoritative accounting
    event per exchange trade. All fills flow through `_account_trades`
    -> Accounting.record_trade -> StateStore.record_fill (atomic +
    idempotent by trade_id). A BUY's executed quantity is converted into
    child SELL orders via `child_sell_qty` on the parent order, so
    repeated reconciliation can never spawn duplicate child sells.
    """

    mode = "abstract"

    def __init__(self, cfg, store: StateStore):
        self.cfg = cfg
        self.store = store
        self.accounting = Accounting(cfg, store)

    def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        parent_order_id: Optional[int] = None,
        target_sell_price: Optional[float] = None,
    ) -> int:
        raise NotImplementedError

    def place_market_sell(self, symbol: str, qty: float, ref_price: float) -> bool:
        raise NotImplementedError

    def cancel_all(self, symbol: str) -> bool:
        raise NotImplementedError

    def sync_fills(self, symbol: str, candle: Optional[Dict], allow_renewal: bool = True) -> None:
        raise NotImplementedError

    def _place_child_sell(self, parent: Dict, qty: float) -> None:
        raise NotImplementedError

    # ----- shared fill accounting (the single accounting path) -----

    def _account_trades(self, order: Dict, trades: List[Dict]) -> float:
        """Record every not-yet-recorded trade exactly once.

        Each trade carries its exchange trade id (`t["id"]`), the
        idempotency key. Returns the quantity newly recorded. Fee and
        quantity always come from the actual trade — never the planned
        order quantity.
        """
        new_qty = 0.0
        for t in trades:
            if self.store.fill_exists(t["id"]):
                continue
            recorded = self.accounting.record_trade(
                order["symbol"], order["id"], order["side"],
                t["price"], t["qty"], t["fee"], t["id"],
            )
            if recorded:
                new_qty += t["qty"]
        return new_qty

    def _spawn_child_sells(self, order: Dict) -> None:
        """Convert executed-but-unconverted BUY quantity into child SELL
        orders. The conversion bookkeeping (`child_sell_qty`) is updated
        atomically with child creation, so the sum of child sells always
        equals the acquired quantity — never more, never duplicated."""
        parent = self.store.get_order(order["id"])
        if parent is None or parent["side"] != "BUY":
            return
        executed = float(parent["filled_qty"] or 0.0)
        converted = float(parent["child_sell_qty"] or 0.0)
        delta = executed - converted
        if delta <= QTY_TOLERANCE:
            return
        self._place_child_sell(parent, delta)

    def _renew_grid_level(self, sell_order: Dict, allow_renewal: bool = True) -> None:
        """Grid renewal: when a child SELL fills, re-place the BUY at the
        same grid level (the parent buy's plan)."""
        if not allow_renewal:
            return
        if not sell_order.get("parent_order_id"):
            return
        parent = self.store.get_order(sell_order["parent_order_id"])
        if parent is None:
            return
        self.place_limit(
            sell_order["symbol"],
            "BUY",
            parent["price"],
            parent["qty"],
            parent_order_id=None,
            target_sell_price=parent["target_sell_price"],
        )


class DryRunExecutor(BaseExecutor):
    """Simulated execution. Never talks to Binance.

    Fill simulation is deterministic: a buy fills when the last CLOSED
    candle's low <= limit price, a sell when the high >= limit price, at
    the limit price with a maker fee. Synthetic trade ids
    (`dry-<client_order_id>`) make the accounting idempotent like live
    trade ids.
    """

    mode = "dry_run"

    def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        parent_order_id: Optional[int] = None,
        target_sell_price: Optional[float] = None,
    ) -> int:
        cid = _new_client_id("dry")
        order_id = self.store.create_order(
            cid, symbol, side, "LIMIT_MAKER", price, qty, self.mode,
            parent_order_id, target_sell_price,
        )
        log.info("dry-run order %s %s %s qty=%s price=%s", symbol, side, "LIMIT_MAKER", qty, price)
        return order_id

    def _place_child_sell(self, parent: Dict, qty: float) -> None:
        cid = _new_client_id("dry")
        self.store.create_child_sell_order(
            cid, parent["symbol"], parent["target_sell_price"], qty,
            parent["id"], self.mode,
        )
        log.info("dry-run child sell %s qty=%s price=%s",
                 parent["symbol"], qty, parent["target_sell_price"])

    def place_market_sell(self, symbol: str, qty: float, ref_price: float) -> bool:
        price = ref_price * (1.0 - self.cfg.slippage_estimate)
        fee = price * qty * max(self.cfg.maker_fee, self.cfg.taker_fee)
        cid = _new_client_id("dry-liq")
        order_id = self.store.create_order(cid, symbol, "SELL", "MARKET", price, qty, self.mode)
        self.store.update_order_status(order_id, "FILLED", qty)
        self._account_trades(
            {"id": order_id, "symbol": symbol, "side": "SELL"},
            [{"id": f"dry-{cid}", "price": price, "qty": qty, "fee": fee}],
        )
        log.info("dry-run liquidation %s qty=%s price=%s", symbol, qty, price)
        return True

    def cancel_all(self, symbol: str) -> bool:
        for order in list(self.store.open_orders(symbol)):
            self.store.update_order_status(order["id"], "CANCELED", order["filled_qty"])
            log.info("dry-run cancel %s order=%s", symbol, order["client_order_id"])
        return len(self.store.open_orders(symbol)) == 0

    def sync_fills(self, symbol: str, candle: Optional[Dict], allow_renewal: bool = True) -> None:
        if candle is None:
            return
        for order in list(self.store.open_orders(symbol)):
            if order["side"] == "BUY" and candle["low"] <= order["price"]:
                trade = {
                    "id": f"dry-{order['client_order_id']}",
                    "price": order["price"],
                    "qty": order["qty"],
                    "fee": order["price"] * order["qty"] * self.cfg.maker_fee,
                }
                self.store.update_order_status(order["id"], "FILLED", order["qty"])
                self._account_trades(order, [trade])
                self._spawn_child_sells(order)
            elif order["side"] == "SELL" and candle["high"] >= order["price"]:
                trade = {
                    "id": f"dry-{order['client_order_id']}",
                    "price": order["price"],
                    "qty": order["qty"],
                    "fee": order["price"] * order["qty"] * self.cfg.maker_fee,
                }
                self.store.update_order_status(order["id"], "FILLED", order["qty"])
                self._account_trades(order, [trade])
                self._renew_grid_level(order, allow_renewal)


class LiveExecutor(BaseExecutor):
    """Real (testnet or gated live) execution with reconciliation.

    Only constructed when DRY_RUN=false. Fill accounting follows the
    exchange trades of each order (myTrades, keyed by trade id) —
    partial fills are accounted as they happen, exactly once. Unknown
    order state after any submission raises OrderUnknownState — the bot
ol instead of guessing.
    """

    mode = "live"

    LIQ_MAX_ATTEMPTS = 3

    def __init__(self, cfg, spot: BinanceSpot, store: StateStore):
        super().__init__(cfg, store)
        self.spot = spot

    def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        parent_order_id: Optional[int] = None,
        target_sell_price: Optional[float] = None,
    ) -> int:
        cid = _new_client_id("ag")
        order_id = self.store.create_order(
            cid, symbol, side, "LIMIT_MAKER", price, qty, self.mode,
            parent_order_id, target_sell_price,
        )
        try:
            resp = self.spot.create_limit_maker_order(symbol, side, price, qty, cid)
        except ExchangeError as exc:
            # Reconcile before any retry — never submit blindly again.
            existing = self._reconcile_by_cid(symbol, cid)
            if existing is None:
                self.store.update_order_status(order_id, "UNKNOWN")
                raise OrderUnknownState(
                    f"order {cid} state unknown after submit failure: {exc}"
                ) from None
            resp = existing
        status = resp.get("status", "NEW")
        self.store.update_order_status(order_id, status, float(resp.get("executedQty") or 0))
        log.info("live order %s %s LIMIT_MAKER qty=%s price=%s status=%s",
                 symbol, side, qty, price, status)
        return order_id

    def _place_child_sell(self, parent: Dict, qty: float) -> None:
        """Submit the child SELL for acquired quantity; the local row and
        the parent's child_sell_qty are created atomically by
        StateStore.create_child_sell_order."""
        cid = _new_client_id("ag")
        local_id = self.store.create_child_sell_order(
            cid, parent["symbol"], parent["target_sell_price"], qty,
            parent["id"], self.mode,
        )
        try:
            resp = self.spot.create_limit_maker_order(
                parent["symbol"], "SELL", parent["target_sell_price"], qty, cid
            )
        except ExchangeError as exc:
            existing = self._reconcile_by_cid(parent["symbol"], cid)
            if existing is None:
                self.store.update_order_status(local_id, "UNKNOWN")
                raise OrderUnknownState(
                    f"child sell {cid} state unknown after submit failure: {exc}"
                ) from None
            resp = existing
        self.store.update_order_status(local_id, resp.get("status", "NEW"), float(resp.get("executedQty") or 0))
        log.info("live child sell %s qty=%s price=%s status=%s",
                 parent["symbol"], qty, parent["target_sell_price"], resp.get("status", "NEW"))

    def place_market_sell(self, symbol: str, qty: float, ref_price: float) -> bool:
        """Liquidation sell with per-attempt client order ids, trade
        reconciliation, and an authoritative balance verification.

        Returns True only when the full quantity executed and the base
        balance actually reflects the sale. Any unknown state raises
        OrderUnknownState (fail-closed); any verification failure returns
        False (the caller must treat the symbol as NOT liquidated).
        """
        base = symbol.split("/")[0]
        tol = max(QTY_TOLERANCE, qty * LIQ_REL_TOLERANCE)
        try:
            balance_before = self.spot.get_balance(base)
        except ExchangeError as exc:
            log.error("balance unavailable before liquidation for %s: %s", symbol, exc)
            self.store.add_risk_event(symbol, "liquidation_verify_failed", "balance unavailable")
            return False

        remaining = qty
        total_executed = 0.0
        for _attempt in range(self.LIQ_MAX_ATTEMPTS):
            if remaining <= tol:
                break
            # Exactly one client order id per attempt, tracked end-to-end.
            cid = _new_client_id("ag-liq")
            local_id = self.store.create_order(cid, symbol, "SELL", "MARKET", ref_price, remaining, self.mode)
            try:
                self.spot.create_market_order(symbol, "SELL", remaining, cid)
            except ExchangeError as exc:
                # The request may or may not have reached the exchange:
                # reconcile this exact client id before doing anything else.
                if self._reconcile_by_cid(symbol, cid) is None:
                    self.store.update_order_status(local_id, "UNKNOWN")
                    raise OrderUnknownState(
                        f"liquidation order {cid} state unknown after submit failure: {exc}"
                    ) from None
            remote = self._wait_terminal(symbol, cid)
            if remote is None:
                self.store.update_order_status(local_id, "UNKNOWN")
                raise OrderUnknownState(f"liquidation order {cid} never reached a terminal state")
            self._record_remote_trades(symbol, remote)
            executed = float(remote.get("executedQty") or 0)
            self.store.update_order_status(local_id, remote.get("status", "FILLED"), executed)
            total_executed += executed
            remaining -= executed

        if remaining > tol:
            self.store.add_risk_event(
                symbol, "liquidation_incomplete", f"remaining={remaining} of {qty}"
            )
            log.error("liquidation incomplete for %s: remaining=%s", symbol, remaining)
            return False

        # Authoritative verification: the exchange base balance must
        # reflect the sale (never increase, and drop by ~ the sold amount;
        # base-asset commissions may drop it slightly further).
        try:
            balance_after = self.spot.get_balance(base)
        except ExchangeError as exc:
            log.error("balance unavailable after liquidation for %s: %s", symbol, exc)
            self.store.add_risk_event(symbol, "liquidation_verify_failed", "balance unavailable")
            return False
        dropped = balance_before - balance_after
        if balance_after > balance_before + tol or dropped < total_executed - tol:
            self.store.add_risk_event(
                symbol,
                "liquidation_balance_mismatch",
                f"before={balance_before} after={balance_after} executed={total_executed}",
            )
            log.error("liquidation balance mismatch for %s: before=%s after=%s executed=%s",
                      symbol, balance_before, balance_after, total_executed)
            return False
        return True

    def cancel_all(self, symbol: str) -> bool:
        for order in list(self.store.open_orders(symbol)):
            cid = order["client_order_id"]
            try:
                self.spot.cancel_order(symbol, cid)
            except ExchangeError as exc:
                existing = self.spot.get_order(symbol, cid)
                if existing is None or existing.get("status") not in _TERMINAL_STATUSES:
                    log.error("cancel failed for %s %s: %s", symbol, cid, exc)
                    return False
            self.store.update_order_status(order["id"], "CANCELED", order["filled_qty"])
        try:
            return len(self.spot.get_open_orders(symbol)) == 0
        except ExchangeError as exc:
            log.error("open-order verification failed for %s: %s", symbol, exc)
            return False

    def sync_fills(self, symbol: str, candle: Optional[Dict], allow_renewal: bool = True) -> None:
        """Reconcile local open orders against the exchange. Trades are
        accounted exactly once (idempotent by trade id) as they happen,
        including partial fills. Unknown orders fail closed."""
        for order in list(self.store.open_orders(symbol)):
            cid = order["client_order_id"]
            remote = self.spot.get_order(symbol, cid)
            if remote is None:
                self.store.update_order_status(order["id"], "UNKNOWN")
                raise OrderUnknownState(f"local open order {cid} not found on exchange")
            self._sync_order_from_remote(symbol, order, remote, allow_renewal)
        remote_open = self.spot.get_open_orders(symbol)
        local_cids = {o["client_order_id"] for o in self.store.open_orders(symbol)}
        for remote_order in remote_open:
            if remote_order.get("clientOrderId") not in local_cids:
                raise OrderUnknownState(
                    f"unknown exchange order {remote_order.get('clientOrderId')} for {symbol}"
                )

    def _sync_order_from_remote(
        self, symbol: str, order: Dict, remote: Dict, allow_renewal: bool
    ) -> None:
        status = remote.get("status", "NEW")
        executed = float(remote.get("executedQty") or 0)
        self.store.update_order_status(order["id"], status, executed)
        trades = []
        for t in self.spot.get_my_trades(symbol, remote.get("orderId")):
            trades.append(
                {
                    "id": str(t.get("id")),
                    "price": float(t["price"]),
                    "qty": float(t["qty"]),
                    "fee": self._fee_in_quote(symbol, t),
                }
            )
        self._account_trades(order, trades)
        if order["side"] == "BUY":
            self._spawn_child_sells(order)
        elif status == "FILLED":
            self._renew_grid_level(order, allow_renewal)

    # ----- helpers -----

    def _reconcile_by_cid(self, symbol: str, cid: Optional[str]) -> Optional[Dict]:
        """Look up an order by its exact client id (bounded polling)."""
        if cid is None:
            return None
        for _ in range(3):
            found = self.spot.get_order(symbol, cid)
            if found is not None:
                return found
            time.sleep(0.2)
        return None

    def _wait_terminal(self, symbol: str, cid: Optional[str]) -> Optional[Dict]:
        if cid is None:
            return None
        for _ in range(5):
            order = self.spot.get_order(symbol, cid)
            if order is None:
                return None
            if order.get("status") in _TERMINAL_STATUSES:
                return order
            time.sleep(0.5)
        return self.spot.get_order(symbol, cid)

    def _record_remote_trades(self, symbol: str, remote_order: Dict) -> None:
        """Account the exchange fills of one remote order (idempotent by
        trade id). Used by the liquidation path, where orders are not
        part of the normal grid reconciliation loop."""
        local = self.store.get_order_by_client_id(remote_order.get("clientOrderId", ""))
        if local is None:
            return
        trades = []
        for t in self.spot.get_my_trades(symbol, remote_order.get("orderId")):
            trades.append(
                {
                    "id": str(t.get("id")),
                    "price": float(t["price"]),
                    "qty": float(t["qty"]),
                    "fee": self._fee_in_quote(symbol, t),
                }
            )
        self._account_trades(local, trades)

    def _fee_in_quote(self, symbol: str, trade: Dict) -> float:
        base, quote = symbol.split("/")
        commission = float(trade.get("commission") or 0)
        asset = trade.get("commissionAsset", "")
        if asset == quote:
            return commission
        if asset == base:
            return commission * float(trade.get("price") or 0)
        log.warning("unhandled commission asset %s on %s (recorded as 0)", asset, symbol)
        return 0.0
