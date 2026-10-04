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


class Accounting:
    """Inventory / fee / realized-PnL bookkeeping shared by executors.

    Conventions (consistent with the equity formula in bot.py):
    - realized PnL records the price difference qty x (sell - avg_cost);
    - fees are recorded separately (every fill's fee);
    - equity = sum(realized) - sum(fees) + unrealized.
    """

    def __init__(self, cfg, store: StateStore):
        self.cfg = cfg
        self.store = store

    def record_fill(
        self,
        symbol: str,
        order_id: int,
        side: str,
        price: float,
        qty: float,
        fee: float,
        trade_id: Optional[str] = None,
    ) -> None:
        st = self.store.get_symbol(symbol)
        inventory = st.inventory_qty if st else 0.0
        avg_cost = st.avg_cost if st else 0.0
        realized = 0.0
        if side == "BUY":
            new_inv = inventory + qty
            avg_cost = ((inventory * avg_cost) + (qty * price)) / new_inv
            inventory = new_inv
        else:  # SELL
            sell_qty = min(qty, inventory) if inventory > 0 else 0.0
            if sell_qty > 0:
                realized = sell_qty * (price - avg_cost)
                inventory -= sell_qty
            if inventory <= 1e-12:
                inventory = 0.0
                avg_cost = 0.0
        self.store.record_fill(
            order_id, symbol, side, price, qty, fee, realized, trade_id
        )
        self.store.update_symbol(
            symbol, inventory_qty=inventory, avg_cost=avg_cost
        )
        log.info(
            "fill %s %s qty=%s price=%s fee=%s realized=%s",
            symbol, side, qty, price, fee, realized,
        )


def _new_client_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


class BaseExecutor:
    """Order lifecycle shared by dry-run and live executors."""

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

    # ----- fill consequences (shared) -----

    def _on_buy_filled(self, order: Dict, fill_price: float, fee: float, trade_id: Optional[str] = None) -> None:
        self.store.update_order_status(order["id"], "FILLED", order["qty"])
        self.accounting.record_fill(
            order["symbol"], order["id"], "BUY", fill_price, order["qty"], fee, trade_id
        )
        # A filled buy immediately spawns its sell at the planned target.
        self.place_limit(
            order["symbol"],
            "SELL",
            order["target_sell_price"],
            order["qty"],
            parent_order_id=order["id"],
        )

    def _on_sell_filled(
        self, order: Dict, fill_price: float, fee: float,
        trade_id: Optional[str] = None, allow_renewal: bool = True,
    ) -> None:
        self.store.update_order_status(order["id"], "FILLED", order["qty"])
        self.accounting.record_fill(
            order["symbol"], order["id"], "SELL", fill_price, order["qty"], fee, trade_id
        )
        # Grid renewal: re-place the buy at the same level for the next cycle.
        if not allow_renewal:
            return
        parent = None
        if order.get("parent_order_id"):
            parent = self.store.get_order(order["parent_order_id"])
        if parent is not None:
            self.place_limit(
                order["symbol"],
                "BUY",
                parent["price"],
                parent["qty"],
                parent_order_id=None,
                target_sell_price=parent["target_sell_price"],
            )


class DryRunExecutor(BaseExecutor):
    """Simulated execution. Never talks to Binance."""

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

    def place_market_sell(self, symbol: str, qty: float, ref_price: float) -> bool:
        price = ref_price * (1.0 - self.cfg.slippage_estimate)
        fee = price * qty * max(self.cfg.maker_fee, self.cfg.taker_fee)
        cid = _new_client_id("dry")
        order_id = self.store.create_order(
            cid, symbol, "SELL", "MARKET", price, qty, self.mode
        )
        self.store.update_order_status(order_id, "FILLED", qty)
        self.accounting.record_fill(symbol, order_id, "SELL", price, qty, fee)
        log.info("dry-run liquidation %s qty=%s price=%s", symbol, qty, price)
        return True

    def cancel_all(self, symbol: str) -> bool:
        for order in list(self.store.open_orders(symbol)):
            self.store.update_order_status(order["id"], "CANCELED", order["filled_qty"])
            log.info("dry-run cancel %s order=%s", symbol, order["client_order_id"])
        return len(self.store.open_orders(symbol)) == 0

    def sync_fills(self, symbol: str, candle: Optional[Dict], allow_renewal: bool = True) -> None:
        """Deterministic fill simulation from the last CLOSED candle:
        a buy fills when candle low <= limit price, a sell when candle
        high >= limit price (fills at the limit price, maker fee)."""
        if candle is None:
            return
        for order in list(self.store.open_orders(symbol)):
            if order["side"] == "BUY" and candle["low"] <= order["price"]:
                fee = order["price"] * order["qty"] * self.cfg.maker_fee
                self._on_buy_filled(order, order["price"], fee)
            elif order["side"] == "SELL" and candle["high"] >= order["price"]:
                fee = order["price"] * order["qty"] * self.cfg.maker_fee
                self._on_sell_filled(order, order["price"], fee, allow_renewal=allow_renewal)


class LiveExecutor(BaseExecutor):
    """Real (testnet or gated live) execution with reconciliation.

    Only constructed when DRY_RUN=false. Unknown order state after any
    submission raises OrderUnknownState — the bot fails closed for the
    symbol instead of guessing.
    """

    mode = "live"

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
            existing = self.spot.get_order(symbol, cid)
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

    def place_market_sell(self, symbol: str, qty: float, ref_price: float) -> bool:
        """Liquidation sell. Returns True only when fully filled."""
        cid = _new_client_id("ag")
        order_id = self.store.create_order(cid, symbol, "SELL", "MARKET", ref_price, qty, self.mode)
        remaining = qty
        for round_no in range(2):
            try:
                self.spot.create_market_order(symbol, "SELL", remaining, cid if round_no == 0 else _new_client_id("ag"))
            except ExchangeError as exc:
                found = self._find_order_any_state(symbol, cid if round_no == 0 else None)
                if found is None:
                    self.store.update_order_status(order_id, "UNKNOWN")
                    raise OrderUnknownState(f"liquidation order state unknown: {exc}") from None
            order = self._wait_terminal(symbol, cid if round_no == 0 else None)
            if order is None:
                self.store.update_order_status(order_id, "UNKNOWN")
                return False
            self._record_trades(symbol, order)
            executed = float(order.get("executedQty") or 0)
            remaining -= executed
            if remaining <= 1e-12:
                self.store.update_order_status(order_id, "FILLED", qty)
                return True
        self.store.update_order_status(order_id, "UNKNOWN")
        return False

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
        """Reconcile local open orders against the exchange before anything
        else is assumed; unknown orders fail closed."""
        for order in list(self.store.open_orders(symbol)):
            cid = order["client_order_id"]
            remote = self.spot.get_order(symbol, cid)
            if remote is None:
                self.store.update_order_status(order["id"], "UNKNOWN")
                raise OrderUnknownState(f"local open order {cid} not found on exchange")
            status = remote.get("status", "NEW")
            self.store.update_order_status(order["id"], status, float(remote.get("executedQty") or 0))
            if status in _TERMINAL_STATUSES:
                self._record_trades(symbol, remote)
                if status == "FILLED":
                    if order["side"] == "BUY":
                        self._on_buy_filled(order, order["price"], 0.0)
                    else:
                        self._on_sell_filled(order, order["price"], 0.0, allow_renewal=allow_renewal)
        remote_open = self.spot.get_open_orders(symbol)
        local_cids = {o["client_order_id"] for o in self.store.open_orders(symbol)}
        for remote_order in remote_open:
            if remote_order.get("clientOrderId") not in local_cids:
                raise OrderUnknownState(
                    f"unknown exchange order {remote_order.get('clientOrderId')} for {symbol}"
                )

    # ----- helpers -----

    def _find_order_any_state(self, symbol: str, cid: Optional[str]) -> Optional[Dict]:
        if cid is None:
            return None
        for _ in range(3):
            found = self.spot.get_order(symbol, cid)
            if found is not None:
                return found
            time.sleep(0.5)
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

    def _record_trades(self, symbol: str, remote_order: Dict) -> None:
        """Persist exchange fills (deduplicated by trade id)."""
        local = self.store.get_order_by_client_id(remote_order.get("clientOrderId", ""))
        if local is None:
            return
        trades = self.spot.get_my_trades(symbol, remote_order.get("orderId"))
        for t in trades:
            fee = self._fee_in_quote(symbol, t)
            self.accounting.record_fill(
                symbol,
                local["id"],
                remote_order.get("side", "BUY"),
                float(t["price"]),
                float(t["qty"]),
                fee,
                trade_id=str(t.get("id")),
            )

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
