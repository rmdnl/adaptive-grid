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
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import grid as grid_mod
import indicators
import strategy as strategy_mod
from config import Config, load_config
from exchange import (
    BinanceSpot,
    DryRunExecutor,
    ExchangeError,
    LiveExecutor,
    OrderUnknownState,
    QTY_TOLERANCE,
)
from risk import BREACH, UNKNOWN, RiskEngine
from state import StateStore, SymbolState

log = logging.getLogger("bot")

CYCLE_SECONDS = 30
KLINE_LIMIT = 200


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


class Bot:
    def __init__(self, cfg: Config, store: StateStore, market: MarketData, executor):
        self.cfg = cfg
        self.store = store
        self.market = market
        self.executor = executor
        self.risk = RiskEngine(cfg, store)
        store.ensure_symbols(list(cfg.pair_list))

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
            except Exception as exc:  # keep the loop alive, mark the symbol
                log.exception("cycle failed for %s", symbol)
                self.store.set_symbol_state(symbol, "ERROR", risk_status="error")
                self.store.add_risk_event(symbol, "cycle_error", str(exc))
        self._update_equity(now)
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
            plan = grid_mod.build_grid(
                symbol,
                self.cfg.grid_mode(symbol),
                snap.last_close,
                snap.atr,
                filters,
                self.cfg,
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
        """True when held inventory is not covered by open sell orders.

        In normal operation every bought unit has exactly one child sell
        until it is sold, so remaining sell quantity always covers the
        inventory. Uncovered inventory can only mean a previous exit or
        kill was interrupted between cancellation and liquidation.
        """
        covered = sum(
            float(o["qty"]) - float(o["filled_qty"] or 0.0)
            for o in self.store.open_orders(symbol)
            if o["side"] == "SELL"
        )
        st = self.store.get_symbol(symbol)
        inventory = float(st.inventory_qty or 0.0) if st else 0.0
        return inventory > covered + QTY_TOLERANCE

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

    # ----- equity / drawdown -----

    def _update_equity(self, now: float) -> None:
        realized = self.store.sum_realized_pnl()
        fees = self.store.sum_fees()
        unrealized = 0.0
        for st in self.store.all_symbols():
            if (st.inventory_qty or 0.0) > 0.0 and st.last_price:
                unrealized += st.inventory_qty * (st.last_price - (st.avg_cost or 0.0))
        equity = self.cfg.start_equity + realized - fees + unrealized
        reference = self.store.get_meta_float("reference_equity")
        if reference is None:
            reference = self.cfg.start_equity
            self.store.set_meta_float("reference_equity", reference)
        if equity > reference:
            reference = equity
            self.store.set_meta_float("reference_equity", reference)
        self.store.set_meta_float("equity", equity)
        if self.risk.drawdown_breach(equity, reference):
            drawdown = (reference - equity) / reference if reference > 0 else 0.0
            self._global_kill(f"max_drawdown_breach dd={drawdown:.4%}")


def build_runtime(cfg: Config, store: StateStore) -> Tuple[Bot, MarketData]:
    spot = BinanceSpot(cfg)
    market = MarketData(spot)
    if cfg.dry_run:
        executor = DryRunExecutor(cfg, store)
    else:
        executor = LiveExecutor(cfg, spot, store)
    return Bot(cfg, store, market, executor), market


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Adaptive grid bot — Binance Spot, dry-run by default"
    )
    parser.add_argument("--env", default=".env", help="path to the .env configuration file")
    parser.add_argument("--db", default="state.db", help="path to the SQLite state database")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.env)
    log.info(
        "startup: environment=%s dry_run=%s pairs=%s timeframe=%s",
        "LIVE" if cfg.allow_live else "TESTNET",
        cfg.dry_run,
        ",".join(cfg.pair_list),
        cfg.indicator_timeframe,
    )
    if cfg.dry_run:
        log.info("DRY RUN: no real orders will be submitted")

    store = StateStore(args.db)
    store.ensure_symbols(list(cfg.pair_list))
    bot, _ = build_runtime(cfg, store)

    if args.once:
        bot.run_once()
        return 0
    _service_loop(bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
