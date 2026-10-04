"""Runtime loop: per-symbol strategy cycle, grid lifecycle, risk enforcement.

Per cycle, for every configured symbol (exit has priority over entry):
1. build the indicator snapshot from CLOSED candles;
2. while a grid is active: check the 15m lower-boundary gate, evaluate
   exit conditions, process fills, and only then place/renew orders;
3. without an active grid: evaluate cooldown and the strict entry gate,
   then build the grid and validate its executable economics;
4. update equity from the fills ledger; a global drawdown >= the hard
   limit triggers the persisted kill switch across all symbols.

DRY_RUN (default) never submits a Binance order — execution is simulated
by DryRunExecutor. Fail-closed rule: any failed cancellation or
liquidation verification stops the symbol in ERROR instead of continuing.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import grid as grid_mod
import indicators
import strategy as strategy_mod
from config import Config, ConfigError, load_config
from exchange import (
    BinanceSpot,
    DryRunExecutor,
    ExchangeError,
    LiveExecutor,
    OrderRejected,
    OrderUnknownState,
    QTY_TOLERANCE,
)
from risk import BREACH, UNKNOWN, RiskEngine
from state import StateStore, SymbolState

log = logging.getLogger("bot")

CYCLE_SECONDS = 30
KLINE_LIMIT = 200


class SessionError(Exception):
    """Raised when the persisted execution session does not match the
    configured mode/environment, or no session capital can be established.
    Fail-closed: the bot refuses to start."""



@dataclass
class CycleView:
    snapshot: strategy_mod.IndicatorSnapshot
    close_15m: Optional[float]
    last_candle: Optional[Dict]


class MarketData:
    """Fetches market data and builds closed-candle indicator snapshots."""

    def __init__(self, spot: BinanceSpot):
        self.spot = spot

    def snapshot(self, symbol: str, cfg: Config, now_ms: int) -> CycleView:
        klines = self.spot.fetch_klines(symbol, cfg.indicator_timeframe, KLINE_LIMIT)
        closed = indicators.closed_candles(klines, now_ms)
        snap = strategy_mod.build_snapshot(
            closed,
            symbol=symbol,
            adx_period=cfg.adx_period,
            rsi_period=cfg.rsi_period,
            bb_period=cfg.bb_period,
            bb_std=cfg.bb_std,
            vo_fast=cfg.vo_fast,
            vo_slow=cfg.vo_slow,
            zscore_period=cfg.zscore_period,
            atr_period=cfg.atr_period,
        )
        close_15m: Optional[float] = None
        try:
            k15 = indicators.closed_candles(self.spot.fetch_klines(symbol, "15m", 2), now_ms)
            if k15:
                close_15m = float(k15[-1]["close"])
        except ExchangeError as exc:
            # Boundary data unavailable → gate reports UNKNOWN (fail-closed).
            log.warning("15m data unavailable for %s: %s", symbol, exc)
        last_candle = closed[-1] if closed else None
        return CycleView(snap, close_15m, last_candle)

    def filters(self, symbol: str):
        return self.spot.get_filters(symbol)

    def avg_price(self, symbol: str) -> Optional[float]:
        """The exchange's weighted-average price (PERCENT_PRICE_BY_SIDE
        reference). None when unavailable — callers fail closed."""
        try:
            data = self.spot.get_avg_price(symbol)
            price = float(data.get("price") or 0)
            return price if price > 0 else None
        except ExchangeError as exc:
            log.warning("reference price unavailable for %s: %s", symbol, exc)
            return None


class Bot:
    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        market: MarketData,
        executor,
        spot: Optional[BinanceSpot] = None,
    ):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.executor = executor
        self.spot = spot
        self.risk = RiskEngine(cfg, store)
        store.ensure_symbols(list(cfg.pair_list))
        # Persist the operating mode so the read-only dashboard displays
        # the runtime's own record (display only — no gate reads it).
        store.set_meta("mode_binance_env", cfg.binance_env)
        store.set_meta("mode_execution", cfg.execution_mode)
        self._initialize_session()

    # ----- execution session (paper/testnet capital) -----

    def _initialize_session(self) -> None:
        """Create or resume the execution session.

        A fresh session derives its capital from the configured
        START_EQUITY (explicit override) or from the Binance TESTNET USDT
        balance. An existing session resumes exactly as persisted — the
        wallet balance is NOT re-imported. A stored session belonging to a
        different execution mode or environment refuses startup (state
        isolation); use --reset-session for an explicit reset.
        """
        store = self.store
        existing_mode = store.get_meta("session_mode")
        existing_env = store.get_meta("session_env")
        if existing_mode is not None or existing_env is not None:
            if existing_mode != self.cfg.execution_mode or existing_env != self.cfg.binance_env:
                raise SessionError(
                    "state database belongs to execution session "
                    f"mode={existing_mode!r} env={existing_env!r}; refusing to start "
                    f"mode={self.cfg.execution_mode!r} env={self.cfg.binance_env!r}. "
                    "Use the matching mode or reset the session explicitly "
                    "(bot.py --reset-session)."
                )
            log.info(
                "resuming session %s (mode=%s env=%s capital=%s)",
                store.get_meta("session_id"),
                existing_mode,
                existing_env,
                store.get_meta_float("session_start_equity"),
            )
            return

        capital: Optional[float] = None
        source = "unknown"
        if self.cfg.start_equity > 0:
            capital = self.cfg.start_equity
            source = "configured START_EQUITY"
        elif self.spot is not None:
            try:
                balance = self.spot.get_balance("USDT")  # signed read-only query
            except ExchangeError as exc:
                raise SessionError(
                    f"cannot initialize session capital from the Binance testnet "
                    f"USDT balance: {exc}"
                ) from None
            if balance <= 0:
                raise SessionError(
                    "Binance testnet USDT balance is 0 — cannot initialize session capital"
                )
            capital = balance
            source = "Binance testnet USDT balance"
            store.set_meta_float("wallet_usdt", balance)
        else:
            raise SessionError(
                "no session capital available: set START_EQUITY in .env "
                "or provide testnet credentials to derive it from the "
                "Binance testnet USDT balance"
            )

        session_id = f"{self.cfg.execution_mode}-{uuid.uuid4().hex[:12]}"
        store.set_meta("session_id", session_id)
        store.set_meta("session_mode", self.cfg.execution_mode)
        store.set_meta("session_env", self.cfg.binance_env)
        store.set_meta_float("session_started_ts", time.time())
        store.set_meta_float("session_start_equity", capital)
        store.set_meta_float("session_initial_cash", capital)
        store.set_meta_float("reference_equity", capital)
        log.info(
            "new session %s: mode=%s env=%s capital=%.2f (%s)",
            session_id, self.cfg.execution_mode, self.cfg.binance_env, capital, source,
        )

    # ----- cycle -----

    def run_once(self) -> None:
        now_ms = int(time.time() * 1000)
        now = now_ms / 1000.0
        kill_active, kill_reason = self.store.global_kill()
        if kill_active:
            log.warning("GLOBAL KILL ACTIVE: %s — no trading", kill_reason)
            for symbol in self.cfg.pair_list:
                self.store.set_symbol_state(symbol, "KILL_ACTIVE")
            self.store.set_runtime("KILL_ACTIVE", now)
            return
        for symbol in self.cfg.pair_list:
            try:
                self._cycle_symbol(symbol, now_ms, now)
            except OrderUnknownState as exc:
                # Unknown order state can never be reconciled: fail closed
                # permanently for this symbol (risk veto, no re-entry).
                log.error("fail-closed (%s): %s", symbol, exc)
                self.store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
                self.store.add_risk_event(symbol, "order_unknown_state", str(exc))
            except OrderRejected as exc:
                # The exchange definitively refused an order (e.g. a filter
                # failure): not unknown, but never retried blindly — the
                # symbol stops for operator attention.
                log.error("order rejected (%s): %s", symbol, exc)
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                self.store.add_risk_event(symbol, "order_rejected", str(exc))
            except Exception as exc:  # keep the loop alive, mark the symbol
                log.exception("cycle failed for %s", symbol)
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                self.store.add_risk_event(symbol, "cycle_error", str(exc))
        self._update_equity(now)
        self._update_wallet()
        self.store.set_runtime("RUNNING", now)

    def _cycle_symbol(self, symbol: str, now_ms: int, now: float) -> None:
        view = self.market.snapshot(symbol, self.cfg, now_ms)
        snap = view.snapshot
        st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)

        # Refresh the market/indicator view; a STOPPED symbol keeps its
        # recorded exit reason (the state must stay authoritative).
        market_fields = dict(
            timeframe=self.cfg.indicator_timeframe,
            last_price=snap.last_close,
            adx=snap.adx,
            rsi=snap.rsi,
            percent_b=snap.percent_b,
            volume_osc=snap.volume_osc,
            zscore=snap.zscore,
            atr=snap.atr,
        )
        if st.risk_status != "stopped":
            market_fields.update(exit_status=0, exit_reason=None)
        self.store.update_symbol(symbol, **market_fields)

        if st.risk_status == "stopped":
            self.store.set_symbol_state(symbol, "STOPPED")
            return
        if strategy_mod.cooldown_active(now, st.cooldown_until):
            self.store.set_symbol_state(symbol, "COOLDOWN")
            return

        exit_decision, entry_decision = strategy_mod.evaluate_signal(snap, self.cfg)

        if self._is_active(symbol, st):
            # 15m lower-boundary gate: independent of exit evaluation,
            # based on the latest CLOSED 15m candle close.
            boundary = self.risk.boundary_status(view.close_15m, st.grid_lower)
            if boundary == BREACH:
                self.store.update_symbol(symbol, exit_status=1, exit_reason="lower_boundary_breach")
                self._exit_symbol(symbol, "lower_boundary_breach", now, cooldown=False)
                return
            if exit_decision.should_exit:
                self.store.update_symbol(
                    symbol, exit_status=1, exit_reason=exit_decision.reason
                )
                self._exit_symbol(symbol, exit_decision.reason, now, cooldown=True)
                return

            allow_renewal = boundary != UNKNOWN
            if boundary == UNKNOWN:
                log.warning(
                    "boundary status UNKNOWN for %s — no new orders this cycle (fail-closed)",
                    symbol,
                )
            self.executor.sync_fills(symbol, view.last_candle, allow_renewal=allow_renewal)
            if self._has_uncovered_inventory(symbol):
                # A previous exit or kill was interrupted (orders cancelled
                # but liquidation never completed): finish the liquidation
                # and stop the symbol — never resume on uncovered inventory.
                log.error("interrupted exit detected for %s — liquidating (fail-closed)", symbol)
                self.store.add_risk_event(
                    symbol, "interrupted_exit_recovery", "inventory without covering sell orders"
                )
                self._exit_symbol(symbol, "interrupted_exit_recovery", now, cooldown=False)
                return
            self.store.set_symbol_state(symbol, "ACTIVE")
            return

        # No active grid: entry path (exit conditions take priority).
        if entry_decision.allowed:
            veto = self.risk.order_veto(symbol)
            if not veto.allowed:
                self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker=veto.reason)
                return
            filters = self.market.filters(symbol)
            reference = self.market.avg_price(symbol)
            plan = grid_mod.build_grid(
                symbol,
                self.cfg.grid_mode(symbol),
                snap.last_close,
                snap.atr,
                filters,
                self.cfg,
                reference_price=reference,
            )
            if not plan.executable:
                self.store.set_symbol_state(
                    symbol,
                    "GRID_BLOCKED",
                    entry_blocker=None,
                    block_reason=plan.block_reason,
                    grid_mode=self.cfg.grid_mode(symbol),
                    gross_pct=plan.gross_pct,
                    net_pct=plan.net_pct,
                )
                log.info("grid blocked for %s: %s", symbol, plan.block_reason)
                return
            self._place_grid(symbol, plan)
            self.store.set_symbol_state(
                symbol,
                "ACTIVE",
                entry_blocker=None,
                block_reason=None,
                grid_mode=plan.mode,
                grid_step=plan.step,
                grid_lower=plan.lower_price,
                gross_pct=plan.gross_pct,
                net_pct=plan.net_pct,
            )
            return

        state = "WAITING" if entry_decision.blocker == "insufficient_data" else "ENTRY_BLOCKED"
        self.store.set_symbol_state(
            symbol, state, entry_blocker=entry_decision.blocker, block_reason=None
        )

    # ----- grid lifecycle -----

    def _is_active(self, symbol: str, st: SymbolState) -> bool:
        if st.strategy_state == "ACTIVE":
            return True
        if self.store.count_open_orders(symbol) > 0:
            return True
        return (st.inventory_qty or 0.0) > 0.0

    def _has_uncovered_inventory(self, symbol: str) -> bool:
        """True when held inventory is neither covered by open sell orders
        nor accounted as pending child-sell conversion.

        In normal operation every bought unit has exactly one child sell
        until it is sold. Uncovered inventory beyond what can still be
        converted means a previous exit or kill was interrupted between
        cancellation and liquidation. (A child sell deferred because its
        price is outside the PERCENT_PRICE_BY_SIDE band leaves the
        quantity pending-conversion — not an interrupted exit.)
        """
        orders = self.store.symbol_orders(symbol)
        covered = sum(
            float(o["qty"]) - float(o["filled_qty"] or 0.0)
            for o in orders
            if o["side"] == "SELL" and o["status"] in ("NEW", "PARTIALLY_FILLED")
        )
        pending_conversion = sum(
            max(0.0, float(o["filled_qty"] or 0.0) - float(o["child_sell_qty"] or 0.0))
            for o in orders
            if o["side"] == "BUY"
        )
        st = self.store.get_symbol(symbol)
        inventory = float(st.inventory_qty or 0.0) if st else 0.0
        return inventory > covered + pending_conversion + QTY_TOLERANCE

    def _place_grid(self, symbol: str, plan: grid_mod.GridPlan) -> None:
        for level in plan.levels:
            self.executor.place_limit(
                symbol,
                "BUY",
                level.buy_price,
                level.qty,
                parent_order_id=None,
                target_sell_price=level.sell_price,
            )
        log.info(
            "grid placed %s mode=%s step=%s gross=%.4f%% net=%.4f%% levels=%d",
            symbol, plan.mode, plan.step, plan.gross_pct * 100, plan.net_pct * 100,
            len(plan.levels),
        )

    def _exit_symbol(self, symbol: str, reason: str, now: float, cooldown: bool) -> None:
        """Automatic exit: cancel → verify → liquidate → verify → record →
        cooldown. Any verification failure is fail-closed (state ERROR)."""
        log.info("exit %s reason=%s", symbol, reason)
        self.store.set_symbol_state(symbol, "EXITING", exit_reason=reason)
        if not self.executor.cancel_all(symbol):
            self.store.add_risk_event(symbol, "cancel_verify_failed", reason)
            self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
            log.error("fail-closed: cancellation verification failed for %s", symbol)
            return
        st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
        inventory = st.inventory_qty or 0.0
        if inventory > 0.0:
            ref_price = st.last_price or 0.0
            if ref_price <= 0:
                self.store.add_risk_event(symbol, "liquidation_verify_failed", "no price reference")
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                return
            liquidated = self.executor.place_market_sell(symbol, inventory, ref_price)
            st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
            if not liquidated or (st.inventory_qty or 0.0) > QTY_TOLERANCE:
                self.store.add_risk_event(symbol, "liquidation_verify_failed", reason)
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                log.error("fail-closed: liquidation verification failed for %s", symbol)
                return
        self.store.add_risk_event(symbol, "auto_exit", reason)
        if cooldown:
            self.store.set_cooldown(symbol, now + self.cfg.cooldown_hours * 3600.0)
            self.store.set_symbol_state(symbol, "COOLDOWN", exit_reason=reason)
        else:
            # Risk stop (e.g. lower-boundary breach): no automatic re-entry.
            self.risk.stop_symbol(symbol, reason)

    def _global_kill(self, reason: str) -> None:
        """Global kill switch.

        1. Latch and persist the kill FIRST (stops new orders globally via
           the risk veto and stays latched across restart, regardless of
           what fails below).
        2. Per symbol: cancel all open orders -> verify -> liquidate held
           inventory -> verify.
        3. Verification failures are fail-closed (symbol ERROR/STOPPED +
           risk events) and never clear the kill.
        """
        log.error("GLOBAL KILL SWITCH: %s", reason)
        self.risk.trigger_global_kill(reason)
        for symbol in self.cfg.pair_list:
            try:
                if not self.executor.cancel_all(symbol):
                    self.store.add_risk_event(symbol, "cancel_verify_failed", reason)
                    self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                    log.error(
                        "fail-closed: cancellation verification failed for %s during global kill",
                        symbol,
                    )
                    continue
                st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                inventory = st.inventory_qty or 0.0
                if inventory > QTY_TOLERANCE:
                    ref_price = st.last_price or 0.0
                    if ref_price <= 0:
                        self.store.add_risk_event(
                            symbol, "liquidation_verify_failed", "no price reference during global kill"
                        )
                        self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                        continue
                    liquidated = self.executor.place_market_sell(symbol, inventory, ref_price)
                    st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                    if not liquidated or (st.inventory_qty or 0.0) > QTY_TOLERANCE:
                        self.store.add_risk_event(symbol, "liquidation_verify_failed", reason)
                        self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                        log.error(
                            "fail-closed: liquidation verification failed for %s during global kill",
                            symbol,
                        )
                        continue
                self.store.set_symbol_state(symbol, "KILL_ACTIVE")
            except OrderUnknownState as exc:
                self.store.add_risk_event(symbol, "order_unknown_state", str(exc))
                self.store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
                log.error("fail-closed (%s) during global kill: %s", symbol, exc)
            except OrderRejected as exc:
                self.store.add_risk_event(symbol, "order_rejected", str(exc))
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                log.error("order rejected (%s) during global kill: %s", symbol, exc)

    def _update_wallet(self) -> None:
        """Display-only testnet wallet telemetry (never used for paper
        equity after session creation; failures are non-fatal)."""
        if self.spot is None or not any(self.cfg.api_credentials):
            return  # no credentials: wallet telemetry unavailable by design
        try:
            self.store.set_meta_float("wallet_usdt", self.spot.get_balance("USDT"))
        except ExchangeError as exc:
            log.warning("testnet wallet balance unavailable: %s", exc)

    # ----- equity / drawdown -----

    def _update_equity(self, now: float) -> None:
        realized = self.store.sum_realized_pnl()
        fees = self.store.sum_fees()
        unrealized = 0.0
        for st in self.store.all_symbols():
            if (st.inventory_qty or 0.0) > 0.0 and st.last_price:
                unrealized += st.inventory_qty * (st.last_price - (st.avg_cost or 0.0))
        # The session capital (paper start equity, derived from the testnet
        # USDT balance or START_EQUITY) anchors the PnL-based equity model.
        capital = self.store.get_meta_float("session_start_equity")
        if capital is None:
            capital = self.cfg.start_equity
        equity = capital + realized - fees + unrealized
        reference = self.store.get_meta_float("reference_equity")
        if reference is None:
            reference = capital
            self.store.set_meta_float("reference_equity", reference)
        if equity > reference:
            reference = equity
            self.store.set_meta_float("reference_equity", reference)
        self.store.set_meta_float("equity", equity)
        if self.risk.drawdown_breach(equity, reference):
            drawdown = (reference - equity) / reference if reference > 0 else 0.0
            self._global_kill(f"max_drawdown_breach dd={drawdown:.4%}")


def build_runtime(cfg: Config, store: StateStore, spot: Optional[BinanceSpot] = None) -> Tuple[Bot, MarketData]:
    if spot is None:
        spot = BinanceSpot(cfg)
    market = MarketData(spot)
    if cfg.execution_mode == "paper":
        # PAPER: internal simulation only — no order ever reaches Binance.
        executor = DryRunExecutor(cfg, store)
    else:
        # TESTNET / LIVE: real execution; the exchange is the source of
        # truth for fills, fees and inventory.
        executor = LiveExecutor(cfg, spot, store)
    return Bot(cfg, store, market, executor, spot=spot), market


def _service_loop(bot: Bot) -> None:
    """Runtime service loop. Per-symbol failures are contained inside
    run_once; anything that escapes (e.g. a database blip during the
    equity update) is logged and retried on the next cycle instead of
    killing the process — the persisted states keep the system safe."""
    while True:
        try:
            bot.run_once()
        except Exception:  # noqa: BLE001 — the service loop must survive
            log.exception("cycle failed; retrying next cycle")
        time.sleep(CYCLE_SECONDS)


def reset_execution_session(cfg: Config, store: StateStore) -> None:
    """Explicit operator reset of the execution session. This is the
    deterministic escape hatch for mode switches: it wipes session-scoped
    trading state (fills, orders, symbol rows, equity, kill state) and a
    fresh session initializes on the next start. Refuses while open
    orders exist; risk events are kept as audit trail."""
    open_orders = store.count_open_orders()
    if open_orders:
        raise SessionError(
            f"refusing to reset: {open_orders} open orders exist — cancel/close them first"
        )
    kill_active, _ = store.global_kill()
    store.reset_session()
    log.warning(
        "SESSION RESET by operator (mode=%s env=%s): trading state, session capital "
        "and kill state (was %s) cleared; a fresh session initializes on next start",
        cfg.execution_mode, cfg.binance_env, "ACTIVE" if kill_active else "inactive",
    )


# block_reason -> operator-facing rejection phrase
GRID_REJECT_PHRASES = {
    "invalid_mode": "invalid grid mode",
    "insufficient_data": "insufficient data (no closed candles / no ATR)",
    "invalid_filters": "invalid exchange filters",
    "reference_price_unavailable": "reference price unavailable",
    "percent_price_band": "percent price band",
    "no_valid_levels": "insufficient valid levels",
    "gross_below_minimum": "gross profit below minimum",
    "net_below_minimum": "net profit below minimum",
}


def _fmt_pct(value: Optional[float], digits: int = 4) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _print_grid_report(
    cfg: Config,
    symbol: str,
    spot,
    view,
    filters,
    reference: Optional[float],
    plan,
    out,
) -> None:
    """Render one symbol's grid economics report. All values come from the
    production grid build (plan) and the production market view - no
    duplicated economics."""
    snap = view.snapshot
    levels = plan.levels
    status = "ACCEPTED" if plan.executable else "REJECTED"
    upper = max((lvl.sell_price for lvl in levels), default=None)

    print(f"SYMBOL: {symbol}", file=out)
    print(f"MODE: {spot.environment}", file=out)
    print(f"GRID MODE: {plan.mode}", file=out)
    print(f"CURRENT PRICE: {snap.last_close if snap.last_close is not None else '—'}", file=out)
    print(f"REFERENCE PRICE: {reference if reference is not None else '—'}", file=out)
    lower = plan.lower_price
    print(f"LOWER PRICE: {lower if lower is not None else '—'}", file=out)
    print(f"UPPER PRICE: {upper if upper is not None else '—'}", file=out)
    print(f"TOTAL GRIDS: {grid_mod.GRID_LEVELS}", file=out)
    print(f"VALID GRID LEVELS: {len(levels)}", file=out)
    print("", file=out)
    print("STEP / SPACING:", file=out)
    if plan.step and snap.last_close:
        spacing_pct = (plan.step / snap.last_close) * 100.0
        print(f"  step = {plan.step:.8g} ({spacing_pct:.4f}% of current price)", file=out)
    else:
        print("  —", file=out)
    print("", file=out)
    print("GROSS PROFIT PER GRID:", file=out)
    if levels:
        print(f"  min = {_fmt_pct(min(l.gross_pct for l in levels))}", file=out)
        print(f"  max = {_fmt_pct(max(l.gross_pct for l in levels))}", file=out)
    else:
        print("  —", file=out)
    print("", file=out)
    print("BUY FEE:", file=out)
    print(f"  maker = {_fmt_pct(cfg.maker_fee, 3)}  taker = {_fmt_pct(cfg.taker_fee, 3)}", file=out)
    print("SELL FEE:", file=out)
    print(f"  maker = {_fmt_pct(cfg.maker_fee, 3)}  taker = {_fmt_pct(cfg.taker_fee, 3)}", file=out)
    print("SLIPPAGE:", file=out)
    print(f"  estimate = {_fmt_pct(cfg.slippage_estimate, 3)}", file=out)
    print("", file=out)
    print("NET PROFIT PER GRID:", file=out)
    if levels:
        nets = [l.net_pct for l in levels]
        print(f"  min = {_fmt_pct(min(nets))}", file=out)
        if len(nets) > 1:
            print(f"  max = {_fmt_pct(max(nets))}", file=out)
            print(f"  average = {_fmt_pct(sum(nets) / len(nets))}", file=out)
        flagged = [lvl for lvl in levels if lvl.net_pct < cfg.min_net_profit_per_grid]
        if flagged:
            print("  below minimum:", file=out)
            for lvl in flagged:
                print(
                    f"    level {lvl.index}: buy {lvl.buy_price:.8g} -> sell {lvl.sell_price:.8g} "
                    f"net {_fmt_pct(lvl.net_pct)}",
                    file=out,
                )
    else:
        print("  — (no valid levels)", file=out)
    print("", file=out)
    print(f"MINIMUM REQUIRED NET: {_fmt_pct(cfg.min_net_profit_per_grid, 2)}", file=out)
    print("", file=out)
    print(f"GRID STATUS: {status}", file=out)
    if not plan.executable:
        phrase = GRID_REJECT_PHRASES.get(plan.block_reason, plan.block_reason or "unknown")
        print(f"REASON: {phrase}", file=out)
    print("", file=out)


def _check_grid(cfg: Config, spot: "BinanceSpot", out=None) -> int:
    """Read-only grid economics validation for every configured symbol.

    Uses the exact production path: MarketData (filters, avg price, closed
    candle ATR) + grid_mod.build_grid (quantization, fees, slippage,
    PERCENT_PRICE_BY_SIDE band). No order is submitted, cancelled or
    created; no state is mutated. Exit code: 0 when every symbol is
    ACCEPTED, 1 otherwise."""
    import sys

    if out is None:
        out = sys.stdout
    market = MarketData(spot)
    now_ms = int(time.time() * 1000)
    results = []
    for symbol in cfg.pair_list:
        try:
            view = market.snapshot(symbol, cfg, now_ms)
            filters = market.filters(symbol)
            reference = market.avg_price(symbol)
        except ExchangeError as exc:
            print(f"SYMBOL: {symbol}", file=out)
            print(f"GRID STATUS: REJECTED", file=out)
            print(f"REASON: market data unavailable ({exc})", file=out)
            print("", file=out)
            results.append((symbol, "REJECTED", None))
            continue
        plan = grid_mod.build_grid(
            symbol,
            cfg.grid_mode(symbol),
            view.snapshot.last_close,
            view.snapshot.atr,
            filters,
            cfg,
            reference_price=reference,
        )
        _print_grid_report(cfg, symbol, spot, view, filters, reference, plan, out)
        worst_net = min((lvl.net_pct for lvl in plan.levels), default=None)
        results.append((symbol, "ACCEPTED" if plan.executable else "REJECTED", worst_net))

    print("GRID VALIDATION SUMMARY", file=out)
    for symbol, status, net in results:
        net_text = _fmt_pct(net) if net is not None else "—"
        print(f"  {symbol:<12} {status:<8} net={net_text}", file=out)
    overall = "REJECTED" if any(s != "ACCEPTED" for _, s, _ in results) else "ACCEPTED"
    print(f"OVERALL: {overall}", file=out)
    return 0 if overall == "ACCEPTED" else 1


def _validate_exchange_access(cfg: Config, spot: BinanceSpot) -> Dict:
    """Startup gate for trading modes: connectivity, auth, permissions,
    clock skew, USDT balance and per-symbol filters. Raises on failure."""
    log.info("validating exchange access (%s) ...", spot.environment)
    access = spot.validate_trading_access(list(cfg.pair_list))
    if access["usdt"] <= 0:
        raise ExchangeError("USDT balance is 0 — cannot trade")
    log.info(
        "exchange access validated: environment=%s USDT=%.2f skew=%dms symbols=%s",
        spot.environment, access["usdt"], access["clock_skew_ms"],
        ",".join(access["symbols"]),
    )
    return access


def _selftest_order_params(filters, reference_price: float, last_close: float) -> Dict:
    """Derive a filter-valid, non-marketable LIMIT_MAKER BUY probe price
    from the ACTUAL exchange filters (never a hardcoded distance).

    The price is the PERCENT_PRICE_BY_SIDE bid-band floor (weighted-average
    reference x bidMultiplierDown), quantized UP to tickSize so it always
    satisfies the band; the quantity is the minimum-notional quantity.
    Every condition is checked locally BEFORE any submission; on any
    violation a ValueError with the exact rejected condition is raised and
    nothing is sent to the exchange.
    """
    from grid import quantize_price_ceil, quantize_qty_ceil, validate_price

    if filters.bid_multiplier_down is None or filters.bid_multiplier_up is None:
        raise ValueError(
            "PERCENT_PRICE_BY_SIDE filter absent — cannot derive a filter-valid probe price"
        )
    if reference_price is None or reference_price <= 0:
        raise ValueError("exchange reference (weighted-average) price unavailable")
    if last_close is None or last_close <= 0:
        raise ValueError("last closed price unavailable (non-marketable check impossible)")

    price = quantize_price_ceil(
        reference_price * filters.bid_multiplier_down, filters.tick_size
    )
    checks: list = [
        (
            "PERCENT_PRICE_BY_SIDE bid band",
            validate_price(filters, "BUY", price, reference_price),
        ),
        (
            "tickSize",
            None if quantize_price_ceil(price, filters.tick_size) == price
            else f"price {price} not tick-aligned",
        ),
    ]

    qty = quantize_qty_ceil(filters.min_notional / price, filters.step_size)
    if filters.min_qty > 0 and qty < filters.min_qty:
        qty = quantize_qty_ceil(filters.min_qty, filters.step_size)
    checks.extend([
        (
            "stepSize",
            None if quantize_qty_ceil(qty, filters.step_size) == qty
            else f"quantity {qty} not step-aligned",
        ),
        (
            "minQty",
            None if filters.min_qty <= 0 or qty >= filters.min_qty
            else f"quantity {qty} below minQty {filters.min_qty}",
        ),
        (
            "minNotional",
            None if qty * price >= filters.min_notional
            else f"notional {qty * price:.8g} below minNotional {filters.min_notional}",
        ),
        # LIMIT_MAKER must be non-marketable: the buy must rest strictly
        # below the observable market (last traded price as the proxy).
        (
            "LIMIT_MAKER non-marketable",
            None if price < last_close
            else f"price {price} would immediately match (last close {last_close})",
        ),
    ])

    violations = [f"{name}: {detail}" for name, detail in checks if detail]
    if violations:
        raise ValueError("; ".join(violations))
    return {"price": price, "qty": qty, "reference_price": reference_price}


def _testnet_order_selftest(cfg: Config, spot: BinanceSpot, symbol: str) -> int:
    """Explicit, flag-gated order-path self-test on Binance Spot Testnet.

    Places ONE far-from-market LIMIT_MAKER BUY whose price is DERIVED from
    the actual exchange filters (the PERCENT_PRICE_BY_SIDE bid-band floor
    against the exchange's weighted-average price), locally validated in
    full before submission, then verified on the book and cancelled. The
    order can never fill and never violates exchange filters.
    Requires EXECUTION_MODE=testnet and is never run automatically.
    """
    if cfg.execution_mode != "testnet" or cfg.allow_live:
        log.error("order self-test is only available in EXECUTION_MODE=testnet")
        return 1
    try:
        _validate_exchange_access(cfg, spot)
        filters = spot.get_filters(symbol)
        klines = spot.fetch_klines(symbol, cfg.indicator_timeframe, 2)
        closed = indicators.closed_candles(klines)
        if not closed:
            raise ExchangeError(f"no closed candles for {symbol}")
        last = float(closed[-1]["close"])
        avg = spot.get_avg_price(symbol)
        reference = float(avg.get("price") or 0)
        log.info(
            "SELFTEST reference: weighted-average price=%s (avgPriceMins filter=%s), last close=%s",
            reference, filters.avg_price_mins, last,
        )
        try:
            params = _selftest_order_params(filters, reference, last)
        except ValueError as exc:
            # Local validation failure -> print the exact rejected
            # condition and DO NOT submit anything.
            log.error("SELFTEST local validation rejected the order: %s", exc)
            return 1
        price, qty = params["price"], params["qty"]
        cid = f"ag-selftest-{uuid.uuid4().hex[:20]}"
        log.warning(
            "SELFTEST: placing filter-valid LIMIT_MAKER BUY %s qty=%s price=%s (reference=%s last=%s)",
            symbol, qty, price, reference, last,
        )
        resp = spot.create_limit_maker_order(symbol, "BUY", price, qty, cid)
        log.info("SELFTEST: order accepted status=%s", resp.get("status"))
        found = None
        for _ in range(3):
            found = spot.get_order(symbol, cid)
            if found is not None:
                break
            time.sleep(0.5)
        if found is None:
            raise ExchangeError("selftest order not found after submission")
        log.info("SELFTEST: order on book status=%s executed=%s", found.get("status"), found.get("executedQty"))
        spot.cancel_order(symbol, cid)
        canceled = spot.get_order(symbol, cid)
        if canceled is None or canceled.get("status") != "CANCELED":
            raise ExchangeError("selftest cancellation could not be verified")
        log.warning("SELFTEST PASSED: order placed, verified and cancelled cleanly")
        return 0
    except ExchangeError as exc:
        log.error("SELFTEST FAILED (fail-closed): %s", exc)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Adaptive grid bot — Binance Spot (paper/testnet by default; live locked)"
    )
    parser.add_argument("--env", default=".env", help="path to the .env configuration file")
    parser.add_argument("--db", default="state.db", help="path to the SQLite state database")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--reset-session", action="store_true",
        help="explicitly reset the execution session (paper/testnet local state; refuses while orders are open)",
    )
    parser.add_argument(
        "--check-exchange", action="store_true",
        help="validate exchange connectivity/auth/filters (read-only) and exit",
    )
    parser.add_argument(
        "--check-grid", action="store_true",
        help="check grid construction and executable net economics (read-only) and exit",
    )
    parser.add_argument(
        "--testnet-order-selftest", metavar="SYMBOL",
        help="place and cancel one far-from-market LIMIT_MAKER order on Binance Spot Testnet, then exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.env)
    log.info(
        "startup: environment=%s execution=%s dry_run=%s pairs=%s timeframe=%s",
        "LIVE" if cfg.allow_live else "TESTNET",
        cfg.execution_mode.upper(),
        cfg.dry_run,
        ",".join(cfg.pair_list),
        cfg.indicator_timeframe,
    )
    if cfg.execution_mode == "paper":
        log.info("PAPER mode: orders are simulated internally; no Binance orders will be submitted")

    # --check-grid is strictly read-only: it must not open the state
    # database, create/resume a session, or submit any order.
    if args.check_grid:
        spot = BinanceSpot(cfg)
        try:
            return _check_grid(cfg, spot)
        except (ExchangeError, SessionError) as exc:
            log.error("grid check failed (fail-closed): %s", exc)
            return 1

    store = StateStore(args.db)
    store.ensure_symbols(list(cfg.pair_list))

    if args.reset_session:
        try:
            reset_execution_session(cfg, store)
            return 0
        except SessionError as exc:
            log.error("session reset refused: %s", exc)
            return 1

    spot = BinanceSpot(cfg)
    try:
        if args.check_exchange:
            _validate_exchange_access(cfg, spot)
            log.warning("exchange access check PASSED")
            return 0
        if args.testnet_order_selftest:
            return _testnet_order_selftest(cfg, spot, args.testnet_order_selftest)
        # Trading modes validate exchange access before anything can run.
        if cfg.execution_mode in ("testnet", "live"):
            _validate_exchange_access(cfg, spot)
    except (ExchangeError, SessionError) as exc:
        log.error("startup refused (fail-closed): %s", exc)
        return 1

    try:
        bot, _ = build_runtime(cfg, store, spot=spot)
    except (SessionError, ConfigError) as exc:
        log.error("startup refused (fail-closed): %s", exc)
        return 1

    if args.once:
        bot.run_once()
        return 0
    _service_loop(bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
