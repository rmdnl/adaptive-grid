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
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import adaptive_grid
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
    candle_15m_time: Optional[int] = None


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
            stoch_rsi_length=cfg.stoch_rsi_length,
            stoch_smooth_k=cfg.stoch_smooth_k,
            stoch_smooth_d=cfg.stoch_smooth_d,
            atr_period=cfg.atr_period,
            adx_regime_lookback=cfg.adx_regime_lookback,
        )
        close_15m: Optional[float] = None
        candle_15m_time: Optional[int] = None
        try:
            k15 = indicators.closed_candles(self.spot.fetch_klines(symbol, "15m", 2), now_ms)
            if k15:
                close_15m = float(k15[-1]["close"])
                candle_15m_time = int(k15[-1]["close_time"])
        except ExchangeError as exc:
            # Boundary data unavailable → gate reports UNKNOWN (fail-closed).
            log.warning("15m data unavailable for %s: %s", symbol, exc)
        last_candle = closed[-1] if closed else None
        return CycleView(snap, close_15m, last_candle, candle_15m_time)

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

    def live_price(self, symbol: str) -> Optional[float]:
        """Current market price from ticker/price endpoint for risk/equity/liquidation.
        
        Separate from indicator close (which uses CLOSED candles only).
        None when unavailable — callers must fail closed for safety-critical operations.
        """
        try:
            return self.spot.get_ticker_price(symbol)
        except ExchangeError as exc:
            log.warning("live price unavailable for %s: %s", symbol, exc)
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
        # The active session's configured symbol list is the dashboard's
        # display scope: a JSON array in PAIR_LIST order. Updated on every
        # startup, so a PAIR_LIST change on a future restart is picked up
        # deterministically.
        store.set_meta(
            "configured_symbols",
            json.dumps(list(cfg.pair_list), separators=(",", ":")),
        )
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
            log.warning("GLOBAL KILL ACTIVE: %s — performing cleanup, no new orders", kill_reason)
            for symbol in self.cfg.pair_list:
                # Phase 6: Global kill recovery after restart.
                # 1. Reconcile exchange state.
                # 2. Cancel remaining open orders.
                # 3. Verify no open orders remain.
                # 4. Verify inventory.
                # 5. Liquidate remaining inventory if necessary.
                # 6. Verify liquidation.
                # 7. Keep the global kill ACTIVE permanently.
                # 8. If any verification fails: FAIL CLOSED.
                # Per-symbol containment: an order-state exception from any
                # step stops THAT symbol fail-closed and never skips the
                # cleanup of the remaining symbols (the kill stays latched,
                # so cleanup retries next cycle).
                try:
                    self.store.set_symbol_state(symbol, "KILL_ACTIVE")
                    try:
                        self.executor.sync_fills(symbol, None, allow_renewal=False)
                    except OrderUnknownState as exc:
                        log.error("fail-closed (%s): kill cleanup reconciliation failed: %s", symbol, exc)
                        self.store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
                        self.store.add_risk_event(symbol, "kill_cleanup_unknown_state", str(exc))
                        continue
                    if not self.executor.cancel_all(symbol):
                        self.store.add_risk_event(symbol, "cancel_verify_failed", kill_reason)
                        self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                        log.error("fail-closed: cancellation verification failed for %s during kill cleanup", symbol)
                        continue
                    st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                    inventory = st.inventory_qty or 0.0
                    if inventory > QTY_TOLERANCE:
                        # Use live market price for liquidation, NOT the stale indicator close.
                        live_price = self.market.live_price(symbol)
                        if live_price is None:
                            self.store.add_risk_event(symbol, "liquidation_verify_failed", "no live price reference during kill cleanup (fail-closed)")
                            self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                            continue
                        liquidated = self.executor.place_market_sell(symbol, inventory, live_price)
                        st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                        if not liquidated or (st.inventory_qty or 0.0) > QTY_TOLERANCE:
                            self.store.add_risk_event(symbol, "liquidation_verify_failed", kill_reason)
                            self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                            log.error("fail-closed: liquidation verification failed for %s during kill cleanup", symbol)
                            continue
                except OrderUnknownState as exc:
                    self.store.add_risk_event(symbol, "kill_cleanup_unknown_state", str(exc))
                    self.store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
                    log.error("fail-closed (%s): unknown order state during kill cleanup: %s", symbol, exc)
                except OrderRejected as exc:
                    self.store.add_risk_event(symbol, "order_rejected", str(exc))
                    self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                    log.error("order rejected (%s) during kill cleanup: %s", symbol, exc)
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

        # Phase 7: Market data freshness protection.
        # Latest strategy candle MUST be CLOSED and fresh.
        max_age = getattr(self.cfg, "max_market_data_age_seconds", 21600.0)
        if snap.last_close is None:
            log.warning("no closed candle for %s — no orders this cycle (fail-closed)", symbol)
            self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker="no_closed_candle")
            return
        if snap.last_candle_time is not None:
            candle_age = (now_ms - snap.last_candle_time) / 1000.0
            if candle_age > max_age:
                log.warning(
                    "stale market data for %s: candle age %.0fs > max %.0fs — no orders this cycle (fail-closed)",
                    symbol, candle_age, max_age
                )
                self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker="stale_market_data")
                return
        # 15m boundary data must independently be CLOSED and fresh.
        close_15m_for_boundary = view.close_15m
        if view.close_15m is not None and view.candle_15m_time is not None:
            candle_15m_age = (now_ms - view.candle_15m_time) / 1000.0
            if candle_15m_age > max_age:
                log.warning(
                    "stale 15m data for %s: candle age %.0fs > max %.0fs — boundary UNKNOWN (fail-closed)",
                    symbol, candle_15m_age, max_age
                )
                close_15m_for_boundary = None
        else:
            # Missing 15m data -> boundary UNKNOWN
            close_15m_for_boundary = None

        # Refresh the market/indicator view; a STOPPED symbol keeps its
        # recorded exit reason (the state must stay authoritative).
        # 
        # last_price: live ticker price for dashboard telemetry and equity
        # valuation. Must NOT be the closed candle price.
        # Indicator/strategy calculations continue to use snap.last_close.
        market_fields = dict(
            timeframe=self.cfg.indicator_timeframe,
            adx=snap.adx,
            plus_di=snap.plus_di,
            minus_di=snap.minus_di,
            stoch_k=snap.stoch_k,
            stoch_d=snap.stoch_d,
            atr=snap.atr,
            # legacy gate indicators are no longer computed (Regime + Recovery)
            rsi=None,
            percent_b=None,
            volume_osc=None,
            zscore=None,
        )
        if st.risk_status != "stopped":
            market_fields.update(exit_status=0, exit_reason=None)

        # Fetch live ticker price for dashboard/equity telemetry.
        # This is SEPARATE from the closed-candle indicator snapshot.
        # Fail-closed: if unavailable, do NOT fabricate — omit last_price
        # so the previous value (if any) persists, and dashboard shows stale.
        live_price = self.market.live_price(symbol)
        if live_price is not None and live_price > 0:
            market_fields["last_price"] = live_price
        else:
            log.debug("live price unavailable for %s — last_price not updated (fail-closed)", symbol)

        self.store.update_symbol(symbol, **market_fields)

        if st.risk_status == "stopped":
            self.store.set_symbol_state(symbol, "STOPPED")
            return
        if strategy_mod.cooldown_active(now, st.cooldown_until):
            # Entry telemetry: a cooldown cycle is an entry attempt blocked
            # by the cooldown gate.
            self._tally_entry_blockers(symbol, ["cooldown"], "cooldown")
            self.store.set_symbol_state(symbol, "COOLDOWN")
            return

        exit_decision, entry_decision = strategy_mod.evaluate_signal(snap, self.cfg)

        if self._is_active(symbol, st):
            # 15m lower-boundary gate: independent of exit evaluation,
            # based on the latest CLOSED 15m candle close against the
            # active adaptive LOWER_PRICE (locked when grid became active).
            # Fall back to configured LOWER_PRICE for legacy static grids.
            adaptive_lower = st.adaptive_lower_price
            configured_lower = self.cfg.lower_price.get(symbol) if hasattr(self.cfg, "lower_price") else None
            effective_lower = adaptive_lower if adaptive_lower is not None else configured_lower
            boundary = self.risk.boundary_status(close_15m_for_boundary, effective_lower)
            if boundary == BREACH:
                # 15m close beyond the boundary is a HARD exit: cancel all,
                # verify, liquidate, verify, hard cooldown.
                self.store.update_symbol(symbol, exit_status=1, exit_reason="lower_boundary_breach")
                self._exit_symbol(symbol, "lower_boundary_breach", now,
                                  cooldown_hours=self.cfg.hard_cooldown_hours)
                return
            if exit_decision.should_exit and exit_decision.severity == "hard":
                # A HARD signal always escalates immediately — even while a
                # soft exit is already in progress.
                self.store.update_symbol(
                    symbol, exit_status=1, exit_reason=exit_decision.reason
                )
                self._exit_symbol(symbol, exit_decision.reason, now,
                                  cooldown_hours=self.cfg.hard_cooldown_hours)
                return

            if st.soft_exit_ts is not None:
                # Soft exit in progress: unfilled BUYs are cancelled, SELLs
                # stay working. Keep reconciling fills; finish into a soft
                # cooldown once everything has sold, and escalate to a HARD
                # exit if inventory still remains after the soft window. A
                # soft signal re-firing here is a no-op (already soft exiting).
                self.executor.sync_fills(symbol, view.last_candle, allow_renewal=False)
                st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                inventory = float(st.inventory_qty or 0.0)
                if inventory <= QTY_TOLERANCE and self.store.count_open_orders(symbol) == 0:
                    self.store.add_risk_event(symbol, "soft_exit_complete", st.exit_reason or "")
                    self._finish_exit(symbol, st.exit_reason or "soft_exit", now,
                                      cooldown_hours=self.cfg.soft_cooldown_hours)
                    return
                if inventory > QTY_TOLERANCE and \
                        now >= (st.soft_exit_ts or now) + self.cfg.soft_cooldown_hours * 3600.0:
                    # SELLs did not clear within the soft window: hard exit.
                    self.store.update_symbol(symbol, exit_status=1, exit_reason="time_stop_escalation")
                    self._exit_symbol(symbol, "time_stop_escalation", now,
                                      cooldown_hours=self.cfg.hard_cooldown_hours)
                    return
                self.store.set_symbol_state(symbol, "ACTIVE")
                return

            if exit_decision.should_exit:
                self.store.update_symbol(
                    symbol, exit_status=1, exit_reason=exit_decision.reason
                )
                self._soft_exit_symbol(symbol, exit_decision.reason, now)
                return

            # TIME STOP: a grid older than HOLD_MAX_HOURS gets a SOFT exit;
            # escalation to hard happens above once soft_exit_ts is set.
            if st.grid_started_ts is not None and \
                    now - st.grid_started_ts >= self.cfg.hold_max_hours * 3600.0:
                self.store.update_symbol(symbol, exit_status=1, exit_reason="time_stop")
                self._soft_exit_symbol(symbol, "time_stop", now)
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
                self._exit_symbol(symbol, "interrupted_exit_recovery", now, cooldown_hours=None)
                return
            self.store.set_symbol_state(symbol, "ACTIVE")
            return

        # No active grid: entry path (exit conditions take priority).
        # Entry telemetry: this cycle evaluated an entry.
        self.store.increment_symbol_counters(symbol, {"entry_evaluations": 1})
        # 15m lower-boundary protection applies when a configured LOWER_PRICE exists
        # (static mode) or when an adaptive grid was previously active (restart recovery).
        # In pure adaptive mode with no prior grid, there's no boundary to check yet.
        if self.cfg.adaptive_grid:
            # Adaptive mode: no static LOWER_PRICE configured.
            # If we have a persisted adaptive_lower_price from a previous grid,
            # use it for boundary protection (restart with active grid handled by _is_active).
            # Otherwise, no boundary check on entry.
            effective_lower = st.adaptive_lower_price if hasattr(st, "adaptive_lower_price") else None
        else:
            # Static mode: use configured LOWER_PRICE
            effective_lower = self.cfg.lower_price.get(symbol) if hasattr(self.cfg, "lower_price") else None

        if effective_lower is not None:
            boundary = self.risk.boundary_status(close_15m_for_boundary, effective_lower)
            if boundary == BREACH:
                self.store.update_symbol(symbol, exit_status=1, exit_reason="lower_boundary_breach")
                self.risk.stop_symbol(symbol, "lower_boundary_breach")
                self.store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
                log.warning("lower boundary breach for %s (no active grid) — symbol STOPPED", symbol)
                return
            if boundary == UNKNOWN:
                log.warning(
                    "boundary status UNKNOWN for %s (no active grid) — no entry this cycle (fail-closed)",
                    symbol,
                )
                self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker="boundary_unknown")
                self.store.update_symbol(symbol, last_entry_blocker="boundary_unknown")
                return

        if entry_decision.allowed:
            veto = self.risk.order_veto(symbol)
            if not veto.allowed:
                self._tally_entry_blockers(symbol, [], veto.reason)
                self.store.increment_symbol_counters(symbol, {"blocked_risk": 1})
                self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker=veto.reason)
                return
            # Regime + Recovery pacing: at most one new grid entry across ALL
            # symbols per MIN_HOURS_BETWEEN_ENTRIES window. 0 disables the
            # pacing gate. Purely a gate — recorded like any other blocker.
            last_entry = self.store.get_meta_float("last_entry_ts_global")
            if last_entry is not None and                     now - last_entry < self.cfg.min_hours_between_entries * 3600.0:
                self._tally_entry_blockers(symbol, [], "min_interval_not_elapsed")
                self.store.set_symbol_state(
                    symbol, "ENTRY_BLOCKED", entry_blocker="min_interval_not_elapsed"
                )
                return
            filters = self.market.filters(symbol)
            reference = self.market.avg_price(symbol)

            # Adaptive grid planning (Phase 1) or static grid from config
            if self.cfg.adaptive_grid:
                # Fetch available USDT balance for quote budget calculation
                try:
                    available_usdt = self.market.spot.get_balance("USDT")
                except ExchangeError as exc:
                    log.warning("USDT balance unavailable for %s: %s", symbol, exc)
                    self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker="balance_unavailable")
                    return
                if available_usdt is None or available_usdt <= 0:
                    self.store.set_symbol_state(symbol, "ENTRY_BLOCKED", entry_blocker="insufficient_balance")
                    return

                # Compute adaptive grid plan
                try:
                    adaptive_plan = adaptive_grid.AdaptiveGridPlanner.plan(
                        symbol=symbol,
                        current_price=snap.last_close,
                        atr=snap.atr,
                        cfg=self.cfg,
                        filters=filters,
                        reference_price=reference,
                        available_usdt=available_usdt,
                    )
                except ValueError as exc:
                    log.info("adaptive grid blocked for %s: %s", symbol, exc)
                    self.store.increment_symbol_counters(symbol, {"blocked_grid": 1})
                    self.store.update_symbol(symbol, last_grid_reject_reason=str(exc))
                    self.store.set_symbol_state(
                        symbol,
                        "GRID_BLOCKED",
                        entry_blocker=None,
                        block_reason=f"adaptive_grid_failed: {exc}",
                        grid_mode=self.cfg.grid_mode(symbol),
                    )
                    return

                # Convert to GridPlan for placement
                plan = grid_mod.GridPlan(
                    symbol=symbol,
                    mode=adaptive_plan.mode,
                    step=adaptive_plan.step,
                    levels=adaptive_plan.levels,
                    lower_price=adaptive_plan.lower_price,
                    upper_price=adaptive_plan.upper_price,
                    gross_pct=adaptive_plan.gross_pct,
                    net_pct=adaptive_plan.net_pct,
                    executable=True,
                    block_reason=None,
                )

                # Place grid and persist adaptive parameters
                if not self._place_grid(symbol, plan):
                    return
                self._tally_entry_success(symbol, now)
                self.store.set_meta_float("last_entry_ts_global", now)
                self.store.update_symbol(
                    symbol,
                    strategy_state="ACTIVE",
                    grid_started_ts=now,
                    entry_blocker=None,
                    block_reason=None,
                    grid_mode=plan.mode,
                    grid_step=plan.step,
                    grid_lower=adaptive_plan.lowest_buy,
                    gross_pct=plan.gross_pct,
                    net_pct=plan.net_pct,
                    # Persist adaptive parameters (locked for active grid)
                    adaptive_lower_price=adaptive_plan.lower_price,
                    adaptive_upper_price=adaptive_plan.upper_price,
                    adaptive_total_grids=adaptive_plan.total_grids,
                    adaptive_quote_budget=adaptive_plan.quote_budget,
                    adaptive_grid_step=adaptive_plan.step,
                    adaptive_reference_price=adaptive_plan.reference_price,
                    adaptive_timeframe=self.cfg.indicator_timeframe,
                )
                return
            else:
                # Static grid from config (legacy mode)
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
                    self.store.increment_symbol_counters(symbol, {"blocked_grid": 1})
                    self.store.update_symbol(symbol, last_grid_reject_reason=plan.block_reason or "unknown")
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
                if not self._place_grid(symbol, plan):
                    return
                self._tally_entry_success(symbol, now)
                self.store.set_meta_float("last_entry_ts_global", now)
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
                    grid_started_ts=now,
                )
                return

        state = "WAITING" if entry_decision.blocker == "insufficient_data" else "ENTRY_BLOCKED"
        self.store.set_symbol_state(
            symbol, state, entry_blocker=entry_decision.blocker, block_reason=None
        )
        # Entry telemetry: per-condition blocker counters. Exit priority does
        # not stop the per-condition accounting: the failed entry conditions
        # are evaluated independently (single source of truth:
        # strategy_mod.entry_blockers) so the counters describe why an entry
        # would not have been made even when an EXIT condition vetoed it.
        # blocked_exit_priority counts the veto itself. Telemetry only —
        # none of this feeds back into any trading decision.
        if entry_decision.blocker == "exit_priority":
            self.store.increment_symbol_counters(symbol, {"blocked_exit_priority": 1})
            self._tally_entry_blockers(
                symbol,
                # keep real condition failures only: a data-availability
                # placeholder must never mask the actual blocker
                [b for b in strategy_mod.entry_blockers(snap, self.cfg)
                 if b in self._BLOCKER_COUNTERS],
                "exit_priority",
            )
        elif entry_decision.blocker != "insufficient_data":
            self._tally_entry_blockers(
                symbol,
                strategy_mod.entry_blockers(snap, self.cfg),
                entry_decision.blocker,
            )

    # ----- grid lifecycle -----

    # entry blocker -> telemetry counter column (read-only tuning statistics;
    # never consulted by any trading decision). Both ADX regime conditions
    # (level and slope) count as the ADX entry-condition failure.
    _BLOCKER_COUNTERS = {
        "adx_not_low": "blocked_adx",
        "adx_rising": "blocked_adx",
        "rsi_not_low": "blocked_rsi",
        "volume_osc_not_positive": "blocked_vo",
        "percent_b_not_low": "blocked_bb",
        "stoch_no_cross": "blocked_stoch_cross",
        "stoch_k_too_high": "blocked_stoch_k",
        "cooldown": "blocked_cooldown",
    }

    def _tally_entry_blockers(self, symbol: str, blockers, last_blocker: Optional[str]) -> None:
        """Record entry-blocker telemetry: per-condition counters plus the
        most recent blocker. Purely observational."""
        counters: Dict[str, int] = {}
        for b in blockers:
            field = self._BLOCKER_COUNTERS.get(b)
            if field:
                counters[field] = counters.get(field, 0) + 1
        if counters:
            self.store.increment_symbol_counters(symbol, counters)
        if last_blocker is not None:
            self.store.update_symbol(symbol, last_entry_blocker=last_blocker)

    def _tally_entry_success(self, symbol: str, now: float) -> None:
        """Record a successful entry in the telemetry counters."""
        self.store.increment_symbol_counters(symbol, {"entries_total": 1})
        self.store.update_symbol(symbol, last_entry_ts=now)

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

    def _place_grid(self, symbol: str, plan: grid_mod.GridPlan) -> bool:
        """Place the executable grid's BUY levels. Returns True when the
        grid was placed, False when the TOTAL_QUOTE_BUDGET hard limit
        rejected it (no orders submitted)."""
        # Phase 2: Enforce TOTAL_QUOTE_BUDGET hard limit.
        budget = self.cfg.total_quote_budget.get(symbol) if hasattr(self.cfg, "total_quote_budget") else None
        if budget is not None:
            # Calculate the sum of executable BUY notional: sum(buy_price * qty) for all levels.
            total_buy_notional = sum(lvl.buy_price * lvl.qty for lvl in plan.levels)
            if total_buy_notional > budget:
                # Log the rejection details for audit.
                log.error(
                    "grid rejected for %s: total buy notional %.8f exceeds TOTAL_QUOTE_BUDGET %.8f",
                    symbol, total_buy_notional, budget
                )
                self.store.increment_symbol_counters(symbol, {"blocked_budget": 1})
                self.store.update_symbol(symbol, last_grid_reject_reason="quote_budget_exceeded")
                self.store.set_symbol_state(
                    symbol,
                    "GRID_BLOCKED",
                    entry_blocker=None,
                    block_reason="quote_budget_exceeded",
                    grid_mode=plan.mode,
                    gross_pct=plan.gross_pct,
                    net_pct=plan.net_pct,
                )
                return False

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
        return True

    def _exit_symbol(self, symbol: str, reason: str, now: float,
                     cooldown_hours: Optional[float]) -> None:
        """HARD exit: cancel all → verify → liquidate → verify → record →
        cooldown (or risk stop when cooldown_hours is None). Any
        verification failure is fail-closed (state ERROR)."""
        log.info("hard exit %s reason=%s", symbol, reason)
        self.store.set_symbol_state(symbol, "EXITING", exit_reason=reason)
        if not self.executor.cancel_all(symbol):
            self.store.add_risk_event(symbol, "cancel_verify_failed", reason)
            self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
            log.error("fail-closed: cancellation verification failed for %s", symbol)
            return
        st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
        inventory = st.inventory_qty or 0.0
        if inventory > 0.0:
            # Use live market price for liquidation, NOT the stale indicator close.
            live_price = self.market.live_price(symbol)
            if live_price is None:
                self.store.add_risk_event(symbol, "liquidation_verify_failed", "no live price reference (fail-closed)")
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                return
            liquidated = self.executor.place_market_sell(symbol, inventory, live_price)
            st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
            if not liquidated or (st.inventory_qty or 0.0) > QTY_TOLERANCE:
                self.store.add_risk_event(symbol, "liquidation_verify_failed", reason)
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                log.error("fail-closed: liquidation verification failed for %s", symbol)
                return
        self.store.add_risk_event(symbol, "auto_exit", reason)
        if cooldown_hours is not None:
            self._finish_exit(symbol, reason, now, cooldown_hours)
        else:
            # Fail-closed stop (e.g. interrupted-exit recovery): no automatic
            # re-entry until explicit operator action.
            self.risk.stop_symbol(symbol, reason)
            self.store.update_symbol(symbol, **self._clear_grid_fields())

    def _clear_grid_fields(self) -> Dict:
        """Grid-scoped fields cleared on every completed exit — they were
        locked for the grid that just exited and must not leak into the next
        cycle. Historical values are preserved implicitly in the fills,
        order and risk-event ledger."""
        return {
            "adaptive_lower_price": None,
            "adaptive_upper_price": None,
            "adaptive_total_grids": None,
            "adaptive_quote_budget": None,
            "adaptive_grid_step": None,
            "adaptive_reference_price": None,
            "adaptive_timeframe": None,
            "grid_mode": None,
            "grid_step": None,
            "grid_lower": None,
            "gross_pct": None,
            "net_pct": None,
            "grid_started_ts": None,
            "soft_exit_ts": None,
        }

    def _soft_exit_symbol(self, symbol: str, reason: str, now: float) -> None:
        """SOFT exit: cancel ONLY unfilled BUY orders (verified), leave SELL
        orders working to fill — never a market sell. The symbol stays
        managed (soft_exit_ts persisted) until every SELL has filled, then
        finishes into a SOFT cooldown. Inventory that still remains after
        the soft window escalates to a HARD exit. Any cancel-verification
        failure is fail-closed (state ERROR)."""
        log.info("soft exit %s reason=%s", symbol, reason)
        self.store.update_symbol(symbol, soft_exit_ts=now)
        if not self.executor.cancel_buys(symbol):
            self.store.add_risk_event(symbol, "cancel_verify_failed", reason)
            self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
            log.error("fail-closed: BUY cancellation verification failed for %s (soft exit)", symbol)
            return
        self.store.add_risk_event(symbol, "soft_exit_started", reason)

    def _finish_exit(self, symbol: str, reason: str, now: float,
                     cooldown_hours: float) -> None:
        """Record a completed exit into its cooldown window and clear all
        grid-scoped fields."""
        self.store.set_cooldown(symbol, now + cooldown_hours * 3600.0)
        self.store.set_symbol_state(symbol, "COOLDOWN", exit_reason=reason,
                                    **self._clear_grid_fields())

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
                    # Use live market price for liquidation, NOT the stale indicator close.
                    live_price = self.market.live_price(symbol)
                    if live_price is None:
                        self.store.add_risk_event(
                            symbol, "liquidation_verify_failed", "no live price reference during global kill (fail-closed)"
                        )
                        self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                        continue
                    liquidated = self.executor.place_market_sell(symbol, inventory, live_price)
                    st = self.store.get_symbol(symbol) or SymbolState(symbol=symbol)
                    if not liquidated or (st.inventory_qty or 0.0) > QTY_TOLERANCE:
                        self.store.add_risk_event(symbol, "liquidation_verify_failed", reason)
                        self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                        log.error(
                            "fail-closed: liquidation verification failed for %s during global kill",
                            symbol,
                        )
                        continue
                # Clear grid-scoped parameters on successful kill cleanup —
                # the killed grid's boundary must not be reused.
                self.store.set_symbol_state(symbol, "KILL_ACTIVE", **self._clear_grid_fields())
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
            if (st.inventory_qty or 0.0) > 0.0:
                # Use live market price for equity valuation, NOT the stale indicator close.
                # Indicator close is for strategy signals; live price is for risk/valuation.
                live_price = self.market.live_price(st.symbol)
                if live_price is None:
                    log.warning("live price unavailable for %s equity valuation — skipping unrealized PnL (fail-closed)", st.symbol)
                    continue
                unrealized += st.inventory_qty * (live_price - (st.avg_cost or 0.0))
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


def _reconcile_state(cfg: Config, spot: "BinanceSpot", store: StateStore, out=None) -> int:
    """Reconcile local state against the exchange BEFORE resuming (testnet).

    Strictly read/report with a no-side-effect guarantee: it mirrors each
    local order to its authoritative exchange status and accounts any
    not-yet-recorded exchange trades (idempotent by trade id), but it
    creates NO new orders, cancels NOTHING and liquidates NOTHING. It also
    checks ledger consistency (inventory == BUY qty - SELL qty; fills are
    unique by trade id).

    Exit code: 0 when clean, 1 when a problem or unknown state is found
    (fail-closed — do NOT resume). PAPER short-circuits with a message.
    """
    import sys

    if out is None:
        out = sys.stdout

    if cfg.execution_mode == "paper":
        print("RECONCILE: PAPER mode — no exchange reconciliation (offline/deterministic).", file=out)
        print("OVERALL: OK", file=out)
        return 0
    if cfg.execution_mode != "testnet":
        print("RECONCILE: only available in EXECUTION_MODE=testnet.", file=out)
        print("OVERALL: FAIL-CLOSED (mode not supported)", file=out)
        return 1

    from exchange import LiveExecutor

    executor = LiveExecutor(cfg, spot, store)
    overall_ok = True
    print("RESTART RECONCILIATION", file=out)
    for symbol in cfg.pair_list:
        print(f"\nSYMBOL: {symbol}", file=out)
        try:
            report = executor.restart_reconcile(symbol)
        except OrderUnknownState as exc:
            # Fail closed for this symbol: stop it, do not resume.
            store.set_symbol_state(symbol, "STOPPED", risk_status="stopped")
            store.add_risk_event(symbol, "reconcile_unknown_state", str(exc))
            print(f"  UNKNOWN: {exc}", file=out)
            print(f"  SYMBOL STATE: STOPPED (fail-closed)", file=out)
            overall_ok = False
            continue
        except ExchangeError as exc:
            # Exchange unavailable / error: cannot verify — fail closed.
            store.add_risk_event(symbol, "reconcile_failed", str(exc))
            print(f"  ERROR: {exc}", file=out)
            print(f"  SYMBOL STATE: UNVERIFIED (fail-closed)", file=out)
            overall_ok = False
            continue

        print(f"  open orders checked: {report['checked']}", file=out)
        print(f"  statuses updated:    {report['updated']}", file=out)
        print(f"  fills recorded:      {report['fills_recorded']}", file=out)
        print(f"  unknown orders:      {report['unknown']}", file=out)

        # Ledger consistency: inventory must equal BUY qty - SELL qty.
        st = store.get_symbol(symbol)
        inventory = float(st.inventory_qty or 0.0) if st else 0.0
        buy_qty, sell_qty = store.fill_quantities(symbol)
        expected = buy_qty - sell_qty
        ledger_ok = abs(inventory - expected) <= QTY_TOLERANCE
        print(
            f"  ledger: inventory={inventory:.8f} buys={buy_qty:.8f} "
            f"sells={sell_qty:.8f} "
            f"{'OK' if ledger_ok else 'MISMATCH'}",
            file=out,
        )
        if not ledger_ok:
            store.add_risk_event(
                symbol, "reconcile_ledger_mismatch",
                f"inventory={inventory} expected={expected}",
            )
            overall_ok = False

    print("", file=out)
    print("OVERALL:", "OK" if overall_ok else "FAIL-CLOSED", file=out)
    return 0 if overall_ok else 1


def _resume_stopped_symbols(cfg: Config, spot: "BinanceSpot", store: StateStore, out=None) -> int:
    """Safely clear symbol-level STOPPED state after strict verification.

    This is an explicit operator recovery command. It NEVER weakens the
    fail-closed design: it only transitions symbols from risk_status="stopped"
    to risk_status="ok" after ALL of the following are verified:

    1. Reconcile exchange/local state (read-only) is completely clean:
       - no unknown local orders (orders on exchange that local DB doesn't know)
       - no unmatched exchange orders (local orders not found on exchange)
       - no local open orders that cannot be verified
    2. Zero inventory for the symbol (inventory_qty == 0)
    3. Zero exchange open orders for the symbol
    4. Global kill switch is INACTIVE
    5. Symbol's current risk_status is exactly "stopped" (not "error", not "ok")
    6. Symbol is NOT in COOLDOWN (cooldown_until must be None or in the past)
    7. Symbol is NOT in ERROR state (risk_status != "error")

    If ANY verification fails for ANY symbol, the operation aborts without
    changing ANY symbol state. The operation is deterministic and idempotent.

    After successful recovery, symbols transition to strategy_state="WAITING"
    with risk_status="ok" — a neutral state where the normal cycle will
    evaluate the strategy again. The adaptive-grid configuration and persisted
    adaptive fields are PRESERVED.

    Exit code: 0 on success (all targeted symbols recovered), 1 on any failure
    (no state changes made). PAPER short-circuits with a message.
    """
    import sys
    import time

    if out is None:
        out = sys.stdout

    if cfg.execution_mode == "paper":
        # PAPER: there is no exchange state to reconcile — every order and
        # fill is local and deterministic. Recovery is still strictly
        # verified locally (kill inactive, zero inventory, ledger consistent,
        # zero open orders) before any state is cleared; any failure aborts
        # with NO state changes.
        kill_active, kill_reason = store.global_kill()
        if kill_active:
            print("GLOBAL KILL ACTIVE:", kill_reason, file=out)
            print("OVERALL: FAIL-CLOSED (global kill active)", file=out)
            return 1

        targets = []
        for symbol in cfg.pair_list:
            st = store.get_symbol(symbol)
            if st is not None and st.risk_status in ("stopped", "error"):
                targets.append(symbol)
        if not targets:
            print("RESUME-STOPPED: PAPER mode — no stopped/error symbols found.", file=out)
            print("OVERALL: OK (nothing to recover)", file=out)
            return 0

        print(f"RESUME-STOPPED: PAPER mode — verifying {len(targets)} symbol(s): {', '.join(targets)}", file=out)
        print("", file=out)
        all_clean = True
        for symbol in targets:
            st = store.get_symbol(symbol)
            symbol_clean = True
            inventory = float(st.inventory_qty or 0.0) if st else 0.0
            buy_qty, sell_qty = store.fill_quantities(symbol)
            ledger_ok = abs(inventory - (buy_qty - sell_qty)) <= QTY_TOLERANCE
            local_open = store.open_orders(symbol)
            print(f"SYMBOL: {symbol}")
            print(f"  ledger: inventory={inventory:.8f} buys={buy_qty:.8f} sells={sell_qty:.8f} "
                  f"{'OK' if ledger_ok else 'MISMATCH'}")
            print(f"  local open orders: {len(local_open)}")
            if not ledger_ok:
                print("  BLOCKED: ledger mismatch")
                symbol_clean = False
            if abs(inventory) > QTY_TOLERANCE:
                print(f"  BLOCKED: non-zero inventory ({inventory})")
                symbol_clean = False
            if len(local_open) > 0:
                print(f"  BLOCKED: {len(local_open)} local open order(s) exist")
                symbol_clean = False
            if symbol_clean:
                print("  VERIFICATION: PASSED")
            else:
                print("  VERIFICATION: FAILED")
            print("", file=out)
            all_clean = all_clean and symbol_clean

        if not all_clean:
            print("OVERALL: FAIL-CLOSED", file=out)
            return 1

        for symbol in targets:
            store.update_symbol(
                symbol,
                risk_status="ok",
                strategy_state="WAITING",
                exit_reason=None,
                exit_status=0,
            )
            print(f"  {symbol}: risk_status=ok strategy_state=WAITING (adaptive params preserved)", file=out)
        print("OVERALL: OK (recovery applied)", file=out)
        return 0
    if cfg.execution_mode != "testnet":
        print("RESUME-STOPPED: only available in EXECUTION_MODE=testnet.", file=out)
        print("OVERALL: FAIL-CLOSED (mode not supported)", file=out)
        return 1

    from exchange import LiveExecutor, OrderUnknownState

    # 1. Global kill switch must be INACTIVE
    kill_active, kill_reason = store.global_kill()
    if kill_active:
        print("GLOBAL KILL ACTIVE:", kill_reason, file=out)
        print("OVERALL: FAIL-CLOSED (global kill active)", file=out)
        return 1

    # 2. Find symbols with risk_status == "stopped"
    stopped_symbols = []
    for symbol in cfg.pair_list:
        st = store.get_symbol(symbol)
        if st is not None and st.risk_status == "stopped":
            stopped_symbols.append(symbol)

    if not stopped_symbols:
        print("RESUME-STOPPED: no symbols with risk_status='stopped' found.", file=out)
        print("OVERALL: OK (nothing to recover)", file=out)
        return 0

    print(f"RESUME-STOPPED: verifying {len(stopped_symbols)} stopped symbol(s): {', '.join(stopped_symbols)}", file=out)
    print("", file=out)

    executor = LiveExecutor(cfg, spot, store)
    all_clean = True
    verification_results = []

    for symbol in stopped_symbols:
        print(f"SYMBOL: {symbol}", file=out)
        symbol_clean = True
        st = store.get_symbol(symbol)

        # Check 2a: Symbol must NOT be in ERROR state
        if st is not None and st.risk_status == "error":
            print(f"  BLOCKED: symbol is in ERROR state (risk_status='error')", file=out)
            symbol_clean = False
            all_clean = False

        # Check 2b: Symbol must NOT be in COOLDOWN
        if st is not None and st.cooldown_until is not None and st.cooldown_until > time.time():
            print(f"  BLOCKED: symbol is in COOLDOWN (until {st.cooldown_until})", file=out)
            symbol_clean = False
            all_clean = False

        # Check 2c: Symbol must have risk_status == "stopped" (already filtered, but double-check)
        if st is None or st.risk_status != "stopped":
            print(f"  BLOCKED: symbol risk_status is not 'stopped' (current: {st.risk_status if st else 'None'})", file=out)
            symbol_clean = False
            all_clean = False

        # Check 3: Reconcile exchange/local state
        try:
            report = executor.restart_reconcile(symbol)
        except OrderUnknownState as exc:
            print(f"  BLOCKED: reconciliation detected unknown order(s): {exc}", file=out)
            symbol_clean = False
            all_clean = False
        except ExchangeError as exc:
            print(f"  BLOCKED: reconciliation failed (exchange error): {exc}", file=out)
            symbol_clean = False
            all_clean = False
        else:
            print(f"  open orders checked: {report['checked']}", file=out)
            print(f"  statuses updated:    {report['updated']}", file=out)
            print(f"  fills recorded:      {report['fills_recorded']}", file=out)
            print(f"  unknown orders:      {report['unknown']}", file=out)

            # Check: reconciliation must be completely clean (unknown == 0)
            if report["unknown"] > 0:
                print(f"  BLOCKED: reconciliation found {report['unknown']} unknown order(s)", file=out)
                symbol_clean = False
                all_clean = False

        # Check 4: Zero inventory
        inventory = float(st.inventory_qty or 0.0) if st else 0.0
        buy_qty, sell_qty = store.fill_quantities(symbol)
        expected = buy_qty - sell_qty
        ledger_ok = abs(inventory - expected) <= QTY_TOLERANCE
        print(
            f"  ledger: inventory={inventory:.8f} buys={buy_qty:.8f} "
            f"sells={sell_qty:.8f} "
            f"{'OK' if ledger_ok else 'MISMATCH'}",
            file=out,
        )
        if not ledger_ok:
            print(f"  BLOCKED: ledger mismatch (inventory={inventory} vs expected={expected})", file=out)
            symbol_clean = False
            all_clean = False
        if abs(inventory) > QTY_TOLERANCE:
            print(f"  BLOCKED: non-zero inventory ({inventory})", file=out)
            symbol_clean = False
            all_clean = False

        # Check 5: Zero exchange open orders
        try:
            exchange_open = spot.get_open_orders(symbol)
        except ExchangeError as exc:
            print(f"  BLOCKED: cannot verify exchange open orders: {exc}", file=out)
            symbol_clean = False
            all_clean = False
            exchange_open = []
        else:
            print(f"  exchange open orders: {len(exchange_open)}", file=out)
            if len(exchange_open) > 0:
                print(f"  BLOCKED: {len(exchange_open)} open order(s) on exchange", file=out)
                for o in exchange_open:
                    print(f"    - {o.get('clientOrderId')}: {o.get('side')} {o.get('type')} @ {o.get('price')} qty={o.get('origQty')}", file=out)
                symbol_clean = False
                all_clean = False

        # Check 6: Zero local open orders
        local_open = store.open_orders(symbol)
        print(f"  local open orders: {len(local_open)}", file=out)
        if len(local_open) > 0:
            print(f"  BLOCKED: {len(local_open)} local open order(s) exist", file=out)
            for o in local_open:
                print(f"    - {o['client_order_id']}: {o['side']} {o['type']} @ {o['price']} qty={o['qty']}", file=out)
            symbol_clean = False
            all_clean = False

        verification_results.append({
            "symbol": symbol,
            "clean": symbol_clean,
            "st": st,
        })

        if symbol_clean:
            print(f"  VERIFICATION: PASSED", file=out)
        else:
            print(f"  VERIFICATION: FAILED", file=out)
        print("", file=out)

    # Summary
    print("VERIFICATION SUMMARY", file=out)
    for vr in verification_results:
        status = "PASS" if vr["clean"] else "FAIL"
        print(f"  {vr['symbol']:<12} {status}", file=out)
    print(f"OVERALL: {'OK' if all_clean else 'FAIL-CLOSED'}", file=out)

    if not all_clean:
        return 1

    # All verifications passed — apply the state transition
    print("", file=out)
    print("APPLYING RECOVERY", file=out)
    for vr in verification_results:
        symbol = vr["symbol"]
        st = vr["st"]
        # Preserve adaptive parameters and all historical data.
        # Only change: risk_status="ok", strategy_state="WAITING", clear exit_reason and exit_status
        store.update_symbol(
            symbol,
            risk_status="ok",
            strategy_state="WAITING",
            exit_reason=None,
            exit_status=0,
            # Do NOT change: cooldown_until, inventory_qty, avg_cost, fills, risk_events, etc.
            # Do NOT change: adaptive_* fields, grid_mode, grid_step, grid_lower, gross_pct, net_pct
        )
        print(f"  {symbol}: risk_status=ok strategy_state=WAITING (adaptive params preserved)", file=out)

    print("", file=out)
    print("RECOVERY COMPLETE: symbols returned to neutral WAITING state.", file=out)
    print("Next cycle will evaluate entry signals normally.", file=out)
    return 0


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
    total_grids: Optional[int] = None,
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
    print(f"TOTAL GRIDS: {total_grids if total_grids is not None else '—'}", file=out)
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
        total_grids: Optional[int] = None
        if cfg.adaptive_grid:
            # Adaptive mode: validate through the same production planner the
            # runtime entry path uses (build_grid with cfg.total_grids is not
            # the production path here — TOTAL_GRIDS is unset by design).
            try:
                available_usdt = spot.get_balance("USDT")
            except ExchangeError as exc:
                print(f"SYMBOL: {symbol}", file=out)
                print(f"GRID STATUS: REJECTED", file=out)
                print(f"REASON: USDT balance unavailable ({exc})", file=out)
                print("", file=out)
                results.append((symbol, "REJECTED", None))
                continue
            try:
                adaptive_plan = adaptive_grid.AdaptiveGridPlanner.plan(
                    symbol=symbol,
                    current_price=view.snapshot.last_close,
                    atr=view.snapshot.atr,
                    cfg=cfg,
                    filters=filters,
                    reference_price=reference,
                    available_usdt=available_usdt,
                )
            except ValueError as exc:
                print(f"SYMBOL: {symbol}", file=out)
                print(f"GRID STATUS: REJECTED", file=out)
                print(f"REASON: {exc}", file=out)
                print("", file=out)
                results.append((symbol, "REJECTED", None))
                continue
            plan = grid_mod.GridPlan(
                symbol=symbol,
                mode=adaptive_plan.mode,
                step=adaptive_plan.step,
                levels=adaptive_plan.levels,
                lower_price=adaptive_plan.lower_price,
                upper_price=adaptive_plan.upper_price,
                gross_pct=adaptive_plan.gross_pct,
                net_pct=adaptive_plan.net_pct,
                executable=True,
                block_reason=None,
            )
            total_grids = adaptive_plan.total_grids
        else:
            plan = grid_mod.build_grid(
                symbol,
                cfg.grid_mode(symbol),
                view.snapshot.last_close,
                view.snapshot.atr,
                filters,
                cfg,
                reference_price=reference,
            )
            total_grids = cfg.total_grids
        _print_grid_report(cfg, symbol, spot, view, filters, reference, plan, out, total_grids)
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
    parser.add_argument(
        "--reconcile", action="store_true",
        help="reconcile local state against the exchange before resuming (read-only; "
             "reports open-order statuses and unreconciled fills, never places orders) and exit",
    )
    parser.add_argument(
        "--resume-stopped", action="store_true",
        help="safely clear symbol-level STOPPED state after strict verification: "
             "reconciles exchange/local state, requires zero inventory, zero open orders, "
             "no unknown orders, no global kill, and only affects risk_status='stopped' symbols",
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

    # --reconcile: read-only pre-resume verification. It never creates a
    # session, places orders, or enters the service loop.
    if args.reconcile:
        try:
            return _reconcile_state(cfg, spot, store)
        except Exception as exc:  # fail closed: report, do not resume
            log.error("reconcile failed (fail-closed): %s", exc)
            return 1

    # --resume-stopped: explicit operator recovery for STOPPED symbols.
    # Strict verification before any state change; no orders placed.
    if args.resume_stopped:
        try:
            return _resume_stopped_symbols(cfg, spot, store)
        except Exception as exc:  # fail closed: report, no state changes
            log.error("resume-stopped failed (fail-closed): %s", exc)
            return 1

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
