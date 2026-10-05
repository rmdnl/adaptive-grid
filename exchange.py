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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional

from grid import ExchangeFilters, validate_price
from state import StateStore

log = logging.getLogger("exchange")

TESTNET_BASE = "https://testnet.binance.vision"
LIVE_BASE = "https://api.binance.com"

_TERMINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}

# Binance error codes that DEFINITIVELY reject an order at submit time:
# the order was never created and retrying the same order cannot succeed
# (e.g. -1013 filter failures, -2010 NEW_ORDER_REJECTED).
_DEFINITIVE_REJECTION_CODES = {-1013, -2010}


def is_definitive_rejection(exc: ExchangeError) -> bool:
    """True when an HTTP 400 from order submission carries a Binance error
    code that definitively rejects the order (it does not exist)."""
    message = str(exc)
    match = re.search(r'"code"\s*:\s*(-\d+)', message)
    if match and int(match.group(1)) in _DEFINITIVE_REJECTION_CODES:
        return True
    return "Filter failure" in message


class ExchangeError(Exception):
    """Request-level failure (HTTP error or exhausted retries)."""


class OrderUnknownState(ExchangeError):
    """The final state of an order submission is unknown (fail-closed)."""


class OrderRejected(ExchangeError):
    """The exchange definitively rejected the order (HTTP 400 with a Binance
    error code, e.g. a filter failure) — the order was never created and a
    retry of the same order cannot succeed."""


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
            # DRY_RUN refuses ORDER-STATE-CHANGING calls at the trading
            # methods, not here: read-only signed queries (account balance)
            # are required to initialize paper capital from the testnet
            # wallet. Trading methods enforce the lock themselves.
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
            except (ValueError, UnicodeDecodeError) as exc:
                # Malformed/non-JSON response body (e.g. an HTML error page):
                # no retry, and always surfaced as an ExchangeError so the
                # fail-closed handling upstream sees one exception type.
                raise ExchangeError(
                    f"{method} {path} returned a malformed response: {exc}"
                ) from None
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
        max_price: Optional[float] = None
        max_qty: Optional[float] = None
        max_notional: Optional[float] = None
        apply_to_market: Optional[bool] = None
        bid_up = bid_down = ask_up = ask_down = None
        avg_price_mins: Optional[int] = None
        for f in symbols[0].get("filters", []):
            ftype = f.get("filterType")
            if ftype == "PRICE_FILTER":
                tick = float(f["tickSize"])
                if "maxPrice" in f and f["maxPrice"]:
                    max_price = float(f["maxPrice"])
            elif ftype == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
                if "maxQty" in f and f["maxQty"]:
                    max_qty = float(f["maxQty"])
            elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("notional", 0)) or 0)
                if "maxNotional" in f and f["maxNotional"]:
                    max_notional = float(f["maxNotional"])
                apply_to_market = bool(f.get("applyToMarket", False))
            elif ftype == "PERCENT_PRICE_BY_SIDE":
                bid_up = float(f["bidMultiplierUp"])
                bid_down = float(f["bidMultiplierDown"])
                ask_up = float(f["askMultiplierUp"])
                ask_down = float(f["askMultiplierDown"])
                avg_price_mins = int(f.get("avgPriceMins") or 0) or None
        if not tick or not step or min_notional is None:
            raise ExchangeError(f"incomplete exchange filters for {symbol}")
        status = symbols[0].get("status")
        if status is not None and status != "TRADING":
            raise ExchangeError(f"{symbol} is not tradable (status={status!r})")
        return ExchangeFilters(
            tick, step, min_notional, min_qty or 0.0,
            max_price=max_price, max_qty=max_qty, max_notional=max_notional,
            apply_to_market=apply_to_market,
            bid_multiplier_up=bid_up, bid_multiplier_down=bid_down,
            ask_multiplier_up=ask_up, ask_multiplier_down=ask_down,
            avg_price_mins=avg_price_mins,
        )

    def get_avg_price(self, symbol: str) -> Dict:
        """The exchange's own weighted-average price — the reference for
        PERCENT_PRICE_BY_SIDE. Never assume the last traded price instead."""
        return self._request(
            "GET",
            "/api/v3/avgPrice",
            {"symbol": exchange_symbol(symbol)},
            retries=2,
        )

    # ----- trading (signed; disabled under DRY_RUN) -----

    def create_limit_maker_order(
        self, symbol: str, side: str, price: float, qty: float, client_order_id: str
    ) -> Dict:
        if self.cfg.dry_run:
            raise ExchangeError("order submission refused: DRY_RUN is enabled")
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
        if self.cfg.dry_run:
            raise ExchangeError("order submission refused: DRY_RUN is enabled")
        params = {
            "symbol": exchange_symbol(symbol),
            "side": side,
            "type": "MARKET",
            "quantity": format(qty, ".8f"),
            "newClientOrderId": client_order_id,
        }
        return self._request("POST", "/api/v3/order", params, signed=True)

    def cancel_order(self, symbol: str, client_order_id: str) -> Dict:
        if self.cfg.dry_run:
            raise ExchangeError("order cancellation refused: DRY_RUN is enabled")
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

    def validate_trading_access(self, symbols: List[str]) -> Dict:
        """Startup gate for trading modes (testnet/live): connectivity,
        clock skew, authentication, account access, trading permission,
        USDT balance and per-symbol filters/status. Raises ExchangeError
        on the first failure — the caller must fail closed."""
        server = self._request("GET", "/api/v3/time", retries=2)
        skew_ms = abs(int(server["serverTime"]) - int(time.time() * 1000))
        if skew_ms > 30_000:
            raise ExchangeError(
                f"local clock differs from Binance server time by {skew_ms}ms "
                "(signed requests would be rejected)"
            )
        account = self.get_account()
        if not account.get("canTrade", False):
            raise ExchangeError("account cannot trade (canTrade=false)")
        usdt = 0.0
        for b in account.get("balances", []):
            if b.get("asset") == "USDT":
                usdt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        for symbol in symbols:
            self.get_filters(symbol)  # validates filters + TRADING status
        return {"usdt": usdt, "clock_skew_ms": skew_ms, "symbols": list(symbols)}


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
        quote_qty: Optional[float] = None,
        commission_asset: Optional[str] = None,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[int] = None,
    ) -> bool:
        recorded = self.store.record_fill(
            order_id, symbol, side, price, qty, fee, trade_id=trade_id,
            quote_qty=quote_qty, commission_asset=commission_asset,
            client_order_id=client_order_id, exchange_order_id=exchange_order_id,
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

    def __init__(self, cfg, store: StateStore, spot: Optional[BinanceSpot] = None):
        self.cfg = cfg
        self.store = store
        self.spot = spot
        self._filters_cache: Dict[str, ExchangeFilters] = {}
        self.accounting = Accounting(cfg, store)

    def _filters_for(self, symbol: str) -> Optional[ExchangeFilters]:
        """Cached exchange filters for band validation (None when no spot
        access is configured or the spot provides no filter support)."""
        if self.spot is None:
            return None
        if symbol not in self._filters_cache:
            try:
                self._filters_cache[symbol] = self.spot.get_filters(symbol)
            except AttributeError:
                return None
        return self._filters_cache[symbol]

    def _child_sell_band_violation(self, symbol: str, price: float) -> Optional[str]:
        """PERCENT_PRICE_BY_SIDE pre-validation for a child SELL against the
        live exchange reference price. Returns the violated condition (the
        placement must be deferred) or None. When the symbol carries no
        percent-price filter there is nothing to enforce."""
        filters = self._filters_for(symbol)
        if filters is None or filters.ask_multiplier_down is None:
            return None
        try:
            reference = float(self.spot.get_avg_price(symbol).get("price") or 0)
        except ExchangeError as exc:
            return f"reference price unavailable: {exc}"
        except AttributeError:
            return None  # spot without reference-price support: cannot validate
        return validate_price(filters, "SELL", price, reference)

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

    def _account_trades(self, order: Dict, trades: List[Dict]) -> int:
        """Record every not-yet-recorded trade exactly once.

        Each trade carries its exchange trade id (`t["id"]`), the
        idempotency key. Returns the number of trades newly recorded. Fee
        and quantity always come from the actual trade — never the planned
        order quantity.
        """
        new_trades = 0
        for t in trades:
            if self.store.fill_exists(t["id"]):
                continue
            recorded = self.accounting.record_trade(
                order["symbol"], order["id"], order["side"],
                t["price"], t["qty"], t["fee"], t["id"],
                quote_qty=t.get("quote_qty"),
                commission_asset=t.get("commission_asset"),
                client_order_id=order.get("client_order_id"),
                exchange_order_id=t.get("exchange_order_id"),
            )
            if recorded:
                new_trades += 1
        return new_trades

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
        violation = self._child_sell_band_violation(
            parent["symbol"], parent["target_sell_price"]
        )
        if violation is not None:
            # Outside the PERCENT_PRICE_BY_SIDE band: defer — the quantity
            # stays unconverted and the spawn is retried on a later cycle
            # against the then-current reference price.
            log.warning(
                "child sell for %s deferred: %s", parent["symbol"], violation
            )
            return
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
            {"id": order_id, "symbol": symbol, "side": "SELL", "client_order_id": cid},
            [{
                "id": f"dry-{cid}", "price": price, "qty": qty, "fee": fee,
                "quote_qty": price * qty,
                "commission_asset": symbol.split("/")[1] if "/" in symbol else "",
            }],
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
                    "quote_qty": order["price"] * order["qty"],
                    "commission_asset": order["symbol"].split("/")[1] if "/" in order["symbol"] else "",
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
                    "quote_qty": order["price"] * order["qty"],
                    "commission_asset": order["symbol"].split("/")[1] if "/" in order["symbol"] else "",
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
    fails closed for the symbol instead of guessing.
    """

    mode = "live"

    LIQ_MAX_ATTEMPTS = 3

    def __init__(self, cfg, spot: BinanceSpot, store: StateStore):
        super().__init__(cfg, store, spot=spot)

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
            if is_definitive_rejection(exc):
                # The exchange refused the order (e.g. a filter failure):
                # it does not exist, and resubmitting cannot succeed.
                self.store.update_order_status(order_id, "REJECTED")
                raise OrderRejected(f"order {cid} rejected: {exc}") from None
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
        violation = self._child_sell_band_violation(
            parent["symbol"], parent["target_sell_price"]
        )
        if violation is not None:
            # Outside the PERCENT_PRICE_BY_SIDE band: defer instead of
            # submitting an order the exchange would refuse — the quantity
            # stays unconverted and the spawn retries on a later cycle.
            log.warning(
                "child sell for %s deferred: %s", parent["symbol"], violation
            )
            return
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
            if is_definitive_rejection(exc):
                self.store.update_order_status(local_id, "REJECTED")
                raise OrderRejected(
                    f"child sell {cid} rejected: {exc}"
                ) from None
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
                if is_definitive_rejection(exc):
                    self.store.update_order_status(local_id, "REJECTED")
                    raise OrderRejected(
                        f"liquidation order {cid} rejected: {exc}"
                    ) from None
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
        """Cancel every open order. Trades that landed before the cancel
        (partial fills, or a cancel racing a fill) are accounted exactly
        once, and each order's local status mirrors the authoritative
        exchange status — a cancel racing a fill records FILLED, never a
        fake CANCELED. Unresolvable orders fail closed."""
        for order in list(self.store.open_orders(symbol)):
            cid = order["client_order_id"]
            try:
                remote = self.spot.cancel_order(symbol, cid)
            except ExchangeError as exc:
                # The cancel itself may have failed because the order is
                # already in a terminal state — reconcile before judging.
                remote = None
                try:
                    remote = self.spot.get_order(symbol, cid)
                except ExchangeError:
                    pass
                if remote is None:
                    log.error("cancel unresolvable for %s %s: %s", symbol, cid, exc)
                    return False
            self._record_remote_trades(symbol, remote)
            status = remote.get("status", "CANCELED")
            self.store.update_order_status(
                order["id"], status, float(remote.get("executedQty") or 0)
            )
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
        self,
        symbol: str,
        order: Dict,
        remote: Dict,
        allow_renewal: bool,
        reconcile_only: bool = False,
    ) -> int:
        """Mirror one local order to its authoritative exchange status and
        account any not-yet-recorded exchange trades (idempotent by trade
        id). Returns the number of fills newly recorded.

        `reconcile_only` (restart reconciliation) suppresses child-sell
        spawning and grid renewal so the pass creates no new orders; those
        resume on the next strategy cycle instead.
        """
        status = remote.get("status", "NEW")
        executed = float(remote.get("executedQty") or 0)
        self.store.update_order_status(order["id"], status, executed)
        # Always account trades (idempotent by trade id) for inventory tracking.
        # The order's filled_qty is set from executedQty above; trades drive
        # inventory and are only recorded once per trade id.
        trades = self._trades_from_exchange(symbol, remote.get("orderId"))
        new_qty = self._account_trades(order, trades)
        if not reconcile_only:
            if order["side"] == "BUY":
                self._spawn_child_sells(order)
            elif status == "FILLED":
                self._renew_grid_level(order, allow_renewal)
        return new_qty

    # ----- restart reconciliation (read/report; no new orders) -----

    def restart_reconcile(self, symbol: str) -> Dict:
        """Reconcile local state against the exchange BEFORE resuming.

        Strict, idempotent, no-side-effect: mirrors each local order to its
        authoritative exchange status (a status the local DB had missed — e.g.
        a partial fill while offline), accounts any exchange trades not yet in
        the ledger (idempotent by trade id), and detects unknown local orders
        or unexpected exchange orders. It creates NO orders, cancels nothing
        and liquidates nothing. Raises on exchange failure or on unknown
        state (fail-closed); the caller must then stop, not resume."""
        report: Dict = {
            "symbol": symbol,
            "checked": 0,
            "updated": 0,
            "fills_recorded": 0,
            "unknown": 0,
            "unknown_orders": [],
        }
        for order in list(self.store.open_orders(symbol)):
            cid = order["client_order_id"]
            remote = self.spot.get_order(symbol, cid)
            if remote is None:
                self.store.update_order_status(order["id"], "UNKNOWN")
                report["unknown"] += 1
                report["unknown_orders"].append(cid)
                continue
            report["fills_recorded"] += self._sync_order_from_remote(
                symbol, order, remote, allow_renewal=False, reconcile_only=True
            )
            report["checked"] += 1
            report["updated"] += 1
        # Detect exchange orders the local DB does not know about.
        local_cids = {o["client_order_id"] for o in self.store.open_orders(symbol)}
        # include ids that just became UNKNOWN so we still flag the unknowns
        local_cids |= set(report["unknown_orders"])
        for remote_order in self.spot.get_open_orders(symbol):
            if remote_order.get("clientOrderId") not in local_cids:
                report["unknown"] += 1
                report["unknown_orders"].append(str(remote_order.get("clientOrderId")))
                log.error(
                    "reconcile %s: unexpected exchange open order %s (no local record)",
                    symbol, remote_order.get("clientOrderId"),
                )
        if report["unknown"]:
            raise OrderUnknownState(
                f"reconciliation of {symbol} detected {report['unknown']} "
                f"unknown order(s): {', '.join(report['unknown_orders'])}"
            )
        return report

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
        trades = self._trades_from_exchange(symbol, remote_order.get("orderId"))
        self._account_trades(local, trades)

    def _trades_from_exchange(self, symbol: str, order_id: Optional[int]) -> List[Dict]:
        """Normalize exchange trades into accounting dicts, carrying full
        provenance (quote quantity, commission asset, exchange order id)."""
        trades = []
        for t in self.spot.get_my_trades(symbol, order_id):
            price = float(t["price"])
            qty = float(t["qty"])
            trades.append(
                {
                    "id": str(t.get("id")),
                    "price": price,
                    "qty": qty,
                    "fee": self._fee_in_quote(symbol, t),
                    "quote_qty": float(t.get("quoteQty") or 0) or price * qty,
                    "commission_asset": str(t.get("commissionAsset") or ""),
                    "exchange_order_id": int(t.get("orderId") or 0) or None,
                }
            )
        return trades

    def _fee_in_quote(self, symbol: str, trade: Dict) -> float:
        base, quote = symbol.split("/")
        commission = float(trade.get("commission") or 0)
        asset = trade.get("commissionAsset", "")
        if asset == quote:
            return commission
        if asset == base:
            return commission * float(trade.get("price") or 0)
        if asset == "BNB":
            # Convert BNB commission to quote using an authoritative market price.
            # Fail closed if conversion price is unavailable.
            try:
                bnb_quote_price = float(self.spot.get_avg_price(f"BNB/{quote}").get("price") or 0)
            except ExchangeError as exc:
                log.error("cannot convert BNB commission for %s: %s", symbol, exc)
                raise ExchangeError(f"BNB fee conversion failed for {symbol}: {exc}") from None
            if bnb_quote_price <= 0:
                raise ExchangeError(f"invalid BNB/{quote} price for fee conversion: {bnb_quote_price}")
            return commission * bnb_quote_price
        # Unknown commission asset: fail closed — never silently record as zero.
        raise ExchangeError(f"unhandled commission asset '{asset}' on {symbol} — cannot convert to quote")
