from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from config_loader import ConfigError, load_config
from fee_model import effective_fees
from grid_engine import build_geometric_grid, validate_grid_profit
from grid_planner import (
    ActivePlan,
    AdaptiveGridPlan,
    PlanBlockReason,
    PlanDecision,
    evaluate_adaptive_grid_plan,
)
from indicators import enrich, latest_valid_row
from market_data import (
    AccountDataError,
    MAX_TICKER_AGE_SECONDS,
    MarketDataError,
    build_account_risk_state,
    fetch_account_commission,
    fetch_account_snapshot,
    fetch_book_ticker,
    fetch_klines,
    fetch_open_orders,
    fetch_symbol_info,
    fetch_ticker_price,
    is_quote_fresh,
    is_ticker_fresh,
    make_client,
)
from grid_eligibility import (
    BlockingReason,
    GridEligibilityDecision,
    GridEligibilityStatus,
    evaluate_grid_eligibility,
)
from market_features import (
    CandleValidationError,
    InsufficientDataError,
    MarketFeatures,
    calculate_market_features,
)
from market_regime import MarketRegime, classify_market_regime
from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperSession, PaperCycleInput
from grid_lifecycle import LifecycleManager
from range_quality import calculate_range_quality
from profit_model import profit_class
from range_engine import auto_range
from risk_engine import (
    account_state_gate, combine, cooldown_gate, daily_profit_lock, equity_dd_kill,
    inventory_gate, market_gate, open_orders_gate, profit_gate,
    open_orders_available_gate, range_break_kill, strict_order_price_gate,
)
from storage import get_state, init_db, record_risk_event, set_state
from symbol_rules import parse_symbol_info, validate_quantized_order_plan

# Intentionally process-local: no historical equity is inferred or persisted.
_SESSION_REFERENCE_EQUITY: Decimal | None = None

# FIX 4B: clock seam for the deterministic paper cycle.  Production uses the
# wall clock; tests may inject a clock aligned to their fixture candle data so
# the orchestrator's market-freshness gate passes deterministically.
_PAPER_CYCLE_CLOCK = None

def _logger(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger=logging.getLogger("adaptive_grid"); logger.setLevel(logging.INFO)
    if logger.handlers: return logger
    formatter=logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh=logging.StreamHandler(); sh.setFormatter(formatter); logger.addHandler(sh)
    fh=logging.FileHandler(path, encoding="utf-8"); fh.setFormatter(formatter); logger.addHandler(fh)
    return logger

def _run_paper_cycle(db_path, cfg, symbol, rules, current_price, kline_df,
                     lower, upper, risk_decision, regime, range_quality_score,
                     candle_index, active_plan=None):
    """Run one deterministic paper cycle through the authoritative orchestrator.

    FIX 4B: this replaces the old non-transactional, per-order
    ``_submit_paper_orders`` loop (F-1: no lifecycle-inventory gate; F-2: no
    atomic cycle transaction).  ``PaperSession.run_cycle`` owns a single cycle
    transaction that atomically applies the lifecycle-integrity gate, the
    inventory-allocation gate, the post-quantization profit gate, order
    submission, deterministic fills and recovery — all-or-nothing.

    FIX 4C (F-4): ``active_plan`` is the main-side active plan read from the
    authoritative lifecycle DB (see ``_lifecycle_active_plan``).  Forwarding it
    to the cycle makes main.py and PaperSession/PaperOrchestrator run the
    planner with identical active-plan inputs, so both paths derive the SAME
    plan decision and the SAME lifecycle generation — no main-invented
    generation counter.

    The clock resolves to ``_PAPER_CYCLE_CLOCK`` (test seam) or, in
    production, a wall clock anchored to the last closed candle so the
    orchestrator's market-freshness gate compares against candle data, not
    the run-time instant.
    """
    global _PAPER_CYCLE_CLOCK
    clock = _PAPER_CYCLE_CLOCK
    if clock is None:
        # Anchor to the last closed candle (plus one candle) so the
        # orchestrator's market-freshness gate compares against the candle
        # data rather than the run-time instant.  When no candle DataFrame is
        # available, fall back to the wall clock.
        if hasattr(kline_df, "iloc") and len(kline_df):
            last_close = kline_df["close_time"].iloc[-1]
            anchor = last_close.to_pydatetime()
            clock = lambda: anchor + timedelta(minutes=15)
        else:
            clock = lambda: datetime.now(timezone.utc)
    accounting = PaperAccountingEngine(
        rules.base_asset,
        rules.quote_asset,
        Decimal(str(cfg["paper"]["initial_base_balance"])),
        Decimal(str(cfg["paper"]["initial_quote_balance"])),
        Decimal(str(cfg["paper"]["maker_fee"])),
        Decimal(str(cfg["paper"]["taker_fee"])),
        str(cfg["paper"]["fee_asset"]),
    )
    # Reuse the single authoritative SQLite file for both the order engine and
    # the lifecycle manager (same-file cycle transaction, no ATTACH needed).
    session = PaperSession(db_path, db_path, accounting, client_order_prefix="AG")
    cycle_input = PaperCycleInput(
        candle_index=candle_index,
        symbol=symbol,
        current_price=current_price,
        kline_df=kline_df,
        # Quote is intentionally not passed: the orchestrator only consumes
        # it for the quote-freshness check, and the regime is supplied
        # pre-computed by main.py's market-intelligence layer.
        quote=None,
        lower_price=lower,
        upper_price=upper,
        # FIX 4C (F-4): forward the lifecycle-derived active plan so the
        # orchestrator's planner runs with the same active-plan inputs main
        # used — both paths share the identical plan decision and lifecycle
        # generation.  main only runs this cycle on a fresh GRID_ALLOWED, at
        # which point active_plan is None; when a plan is active it is the
        # authoritative lifecycle value, never a main-invented counter.
        active_plan=active_plan,
        regime=regime,
        range_quality_score=range_quality_score,
        cfg=cfg,
        maker_fee=Decimal(str(cfg["paper"]["maker_fee"])),
        taker_fee=Decimal(str(cfg["paper"]["taker_fee"])),
        fee_asset=str(cfg["paper"]["fee_asset"]),
        risk_decision=risk_decision,
        clock=clock,
        dry_run=True,
        rules=rules,
    )
    return session.run_cycle(cycle_input)

def _lifecycle_active_plan(db_path):
    """Read the authoritative active plan from the lifecycle manager DB.

    FIX 4C (F-4): the lifecycle database is the SINGLE source of truth for the
    active plan and its generation namespace.  ``main.py`` does NOT maintain
    an independent plan-generation counter; it reads what the lifecycle
    manager owns (the same rows the orchestrator reads).  ``None`` when the
    lifecycle state has no active plan (NO_ACTIVE_GRID / BLOCKED / empty).

    The returned generation is ``LifecycleManager.get_generation()`` (the
    authoritative manager generation), not a main-invented value, so any
    conflicting legacy ``bot_state`` value cannot pollute the executable
    generation.
    """
    manager = LifecycleManager(db_path)
    active = manager.get_active_plan()
    if active is None:
        return None
    # generation = the authoritative manager generation (max recorded), which
    # for an ACTIVE plan equals the active plan's own generation.
    generation = manager.get_generation()
    return ActivePlan(
        plan_id=active.plan_id,
        candidate_lower=active.candidate_lower,
        candidate_upper=active.candidate_upper,
        grid_step=active.grid_step,
        grid_count=active.grid_count,
        regime=MarketRegime(active.regime),
        range_quality_score=active.range_quality_score,
        candle_index=active.candle_index,
        generation=generation,
    )

def main():
    global _SESSION_REFERENCE_EQUITY
    load_dotenv()
    try:
        cfg=load_config()
    except ConfigError as exc:
        print(f"CONFIG BLOCK: {exc}")
        return 2
    if not cfg["environment"]["dry_run"]:
        raise RuntimeError("DRY_RUN must remain enabled; live execution is disabled")

    db_path=cfg["logging"]["sqlite_path"]; init_db(db_path)
    logger=_logger(cfg["logging"]["log_path"])

    mode=cfg["environment"]["mode"]; symbol=cfg["symbol"]
    client=make_client(mode, os.getenv("BINANCE_API_KEY",""), os.getenv("BINANCE_API_SECRET",""))

    symbol_info=fetch_symbol_info(client,symbol)
    rules=parse_symbol_info(symbol_info)

    df=fetch_klines(client,symbol,cfg["timeframe"],cfg["range"]["lookback"],drop_incomplete=True)
    enriched=enrich(df); last=latest_valid_row(enriched)
    ticker=None; ticker_error=None
    try:
        ticker=fetch_ticker_price(client,symbol)
        if not is_ticker_fresh(ticker, MAX_TICKER_AGE_SECONDS):
            raise MarketDataError(f"Ticker price is stale or invalid for {ticker.symbol}")
    except MarketDataError as exc:
        ticker_error=str(exc)
        logger.error("TICKER DATA BLOCK: %s",ticker_error)

    book_quote=None; book_quote_error=None
    if "market_intelligence" in cfg:
        try:
            book_quote=fetch_book_ticker(client,symbol)
            max_quote_age=int(
                cfg["market_intelligence"].get("liquidity",{}).get(
                    "max_quote_ticker_age_seconds", 10
                )
            )
            if not is_quote_fresh(book_quote, max_quote_age):
                raise MarketDataError(f"Book ticker quote is stale for {symbol}")
        except Exception as exc:
            book_quote_error=str(exc)
            logger.error("BOOK TICKER DATA BLOCK: %s",book_quote_error)

    account_snapshot=None; account_risk=None; account_error=None
    try:
        account_snapshot=fetch_account_snapshot(client,rules.base_asset,rules.quote_asset)
    except AccountDataError as exc:
        account_error=str(exc)
        logger.error("ACCOUNT DATA BLOCK: %s",account_error)

    open_orders=None; open_orders_error=None
    try:
        open_orders=fetch_open_orders(client,symbol)
    except Exception as exc:
        # Reconciliation is independent of account-state validation and cannot
        # be replaced by a persisted count or an assumed empty response.
        open_orders_error=str(exc)
        logger.error("OPEN-ORDER RECONCILIATION BLOCK: %s",open_orders_error)

    # Ticker, account, and open orders have now each been independently queried.
    # A ticker failure still stops this run: there is no safe price fallback.
    if ticker is None:
        raise RuntimeError(f"Ticker data unavailable: {ticker_error}")
    if account_snapshot is not None:
        try:
            account_risk=build_account_risk_state(
                account_snapshot,ticker.price,_SESSION_REFERENCE_EQUITY,
            )
            if _SESSION_REFERENCE_EQUITY is None:
                _SESSION_REFERENCE_EQUITY=account_risk.reference_equity
        except AccountDataError as exc:
            account_error=str(exc)
            logger.error("ACCOUNT RISK BLOCK: %s",account_error)

    commission_payload, fee_source_raw=fetch_account_commission(client,symbol)
    fees=effective_fees(
        commission_payload,
        cfg["fees"]["maker_fee_fallback"],
        cfg["fees"]["taker_fee_fallback"],
    )
    fee_source=fees.source if commission_payload else fee_source_raw

    if cfg["range"]["mode"]=="manual":
        lower=Decimal(str(cfg["range"]["lower_price"]))
        upper=Decimal(str(cfg["range"]["upper_price"]))
        range_quality=100.0; range_reason="MANUAL_RANGE"; range_approved=True
        position_in_range=float((Decimal(str(last["close"]))-lower)/(upper-lower))
    else:
        candidate=auto_range(enriched,**cfg["range"]["auto"])
        lower, upper = candidate.lower, candidate.upper
        range_quality, range_reason = candidate.quality, candidate.reason
        range_approved, position_in_range = candidate.approved, candidate.position_in_range

    current_price=ticker.price

    market_intelligence_decision: GridEligibilityDecision | None = None
    if "market_intelligence" in cfg:
        try:
            features = calculate_market_features(
                df=df,
                quote=book_quote,
                lower_price=lower,
                upper_price=upper,
                symbol=symbol,
                config=cfg,
            )
            regime, regime_reason = classify_market_regime(features, cfg)
            quality_res = calculate_range_quality(features, cfg)
            market_intelligence_decision = evaluate_grid_eligibility(
                features=features,
                regime=regime,
                range_quality=quality_res,
                current_price=current_price,
                lower_price=lower,
                upper_price=upper,
                config=cfg,
                extra_diagnostics={"regime_reason": regime_reason},
            )
        except CandleValidationError as exc:
            is_insufficient = isinstance(exc, InsufficientDataError)
            regime, regime_reason = classify_market_regime(
                None,
                cfg,
                is_insufficient_data=is_insufficient,
                is_invalid_data=not is_insufficient,
                error_message=str(exc),
            )
            market_intelligence_decision = evaluate_grid_eligibility(
                features=None,
                regime=regime,
                range_quality=None,
                current_price=current_price,
                lower_price=lower,
                upper_price=upper,
                config=cfg,
                is_insufficient_data=is_insufficient,
                is_invalid_data=not is_insufficient,
                extra_diagnostics={"error": str(exc), "regime_reason": regime_reason},
            )
        except Exception as exc:
            regime, regime_reason = classify_market_regime(
                None,
                cfg,
                is_invalid_data=True,
                error_message=str(exc),
            )
            market_intelligence_decision = evaluate_grid_eligibility(
                features=None,
                regime=regime,
                range_quality=None,
                current_price=current_price,
                lower_price=lower,
                upper_price=upper,
                config=cfg,
                is_invalid_data=True,
                extra_diagnostics={"error": str(exc)},
            )

    # Phase 5A: adaptive grid planner — read-only candidate-plan decision layer.
    # Consumes Phase 4 market intelligence; never places or cancels orders.
    adaptive_plan: AdaptiveGridPlan | None = None
    active_plan: ActivePlan | None = None
    allow_new_orders = True
    if "adaptive_planner" in cfg and market_intelligence_decision is not None:
        eval_index = int(get_state(db_path, "adaptive_eval_index") or "0") + 1
        set_state(db_path, "adaptive_eval_index", str(eval_index))

        # FIX 4C (F-4): the active plan is read EXCLUSIVELY from the lifecycle
        # manager database — the single authoritative source for plan identity
        # and its generation namespace.  main.py keeps NO independent
        # plan-generation counter; a stale/conflicting bot_state value cannot
        # pollute the executable generation because main never reads it back.
        active_plan = _lifecycle_active_plan(db_path)

        base_available = Decimal(str(cfg["paper"]["initial_base_balance"]))
        try:
            adaptive_plan = evaluate_adaptive_grid_plan(
                pair=symbol,
                regime=market_intelligence_decision.regime,
                range_quality_score=market_intelligence_decision.range_quality_score,
                current_price=current_price,
                configured_lower=lower,
                configured_upper=upper,
                available_base_inventory=base_available,
                cfg=cfg,
                active_plan=active_plan,
                current_candle_index=eval_index,
            )
        except Exception as exc:
            logger.error("ADAPTIVE PLANNER BLOCK: %s", exc)
            adaptive_plan = AdaptiveGridPlan(
                plan_id="plan_error",
                pair=symbol,
                regime=market_intelligence_decision.regime,
                range_quality_score=market_intelligence_decision.range_quality_score,
                candidate_lower=lower,
                candidate_upper=upper,
                grid_type="GEOMETRIC",
                grid_step=Decimal(str(cfg["grid"]["step_pct"])),
                grid_count=0,
                levels=(),
                total_quote_budget=Decimal(str(cfg["execution"]["total_quote_budget"])),
                buy_quote_budget=Decimal("0"),
                required_base_inventory=Decimal("0"),
                available_base_inventory=base_available,
                inventory_sufficient=False,
                estimated_net_profit_per_grid=Decimal("0"),
                decision=PlanDecision.GRID_BLOCKED,
                reasons=(PlanBlockReason.INVALID_MARKET_DATA,),
            )

        if adaptive_plan.decision == PlanDecision.GRID_ALLOWED:
            allow_new_orders = True
            # FIX 4C (F-4): write only an INFORMATIONAL plan-identity mirror
            # for observability.  main.py owns NO generation namespace: the
            # executable generation is owned exclusively by the lifecycle
            # manager (advanced atomically in the orchestrator's cycle
            # transaction via activate_plan) and is never read back from
            # bot_state.  This mirror deliberately carries no generation key.
            set_state(db_path, "last_active_plan", {
                "plan_id": adaptive_plan.plan_id,
                "candidate_lower": str(adaptive_plan.candidate_lower),
                "candidate_upper": str(adaptive_plan.candidate_upper),
                "grid_step": str(adaptive_plan.grid_step),
                "grid_count": adaptive_plan.grid_count,
                "regime": adaptive_plan.regime.value,
                "range_quality_score": str(adaptive_plan.range_quality_score),
                "candle_index": eval_index,
            })
        else:
            # KEEP_CURRENT_PLAN / RECONFIGURATION_REQUIRED / GRID_BLOCKED:
            # current plan stays in place; no new orders are placed.
            allow_new_orders = False

    levels=[]; effective_upper=upper; validation=None; plan_validation=None; grid_allowed=False; grid_reason="NOT_BUILT"
    if lower > 0 and upper > lower:
        try:
            levels,effective_upper=build_geometric_grid(
                lower,upper,cfg["grid"]["step_pct"],
                min_cells=int(cfg["grid"]["min_cells"]),
                max_levels=int(cfg["grid"]["max_levels"]),
            )
            sell_fee=fees.maker if cfg["execution"]["prefer_limit_maker"] else fees.taker
            validation=validate_grid_profit(
                levels,fees.maker,sell_fee,
                cfg["fees"]["slippage_roundtrip_pct"],
                cfg["grid"]["hard_min_net_pct"],
            )
            plan_validation=validate_quantized_order_plan(
                levels,
                rules,
                cfg["execution"]["order_quote_size"],
                current_price,
                fees.maker,
                sell_fee,
                cfg["fees"]["slippage_roundtrip_pct"],
                cfg["grid"]["hard_min_net_pct"],
                cfg["execution"]["max_open_orders"],
            )
            effective_upper=plan_validation.effective_upper
            grid_allowed=validation.allowed and plan_validation.allowed
            grid_reason=(
                validation.reason if not validation.allowed else plan_validation.reason
            )
        except (ValueError,ArithmeticError) as exc:
            grid_reason=f"GRID_BUILD_BLOCK:{exc}"
    else:
        grid_reason="INVALID_RANGE"

    if account_error:
        grid_allowed=False
        grid_reason=f"{grid_reason}|ACCOUNT_DATA_UNAVAILABLE"

    decisions=[
        profit_gate(
            plan_validation.min_net_pct if plan_validation else Decimal("0"),
            cfg["grid"]["hard_min_net_pct"],
        ),
        market_gate(last,cfg["market_filter"]),
        strict_order_price_gate(lower,effective_upper,current_price),
        range_break_kill(lower,effective_upper,current_price,cfg["risk"]["range_break_buffer_pct"]),
        cooldown_gate(False),
        daily_profit_lock(Decimal("0"),cfg["risk"]["daily_profit_lock_pct"]),
    ]
    if account_risk:
        decisions.extend((
            equity_dd_kill(account_risk.drawdown_pct,cfg["risk"]["max_equity_drawdown_pct"]),
            inventory_gate(account_risk.inventory_pct,cfg["execution"]["max_inventory_pct"]),
        ))
    else:
        decisions.append(account_state_gate(False))
    if open_orders is None:
        decisions.append(open_orders_available_gate(False))
    else:
        decisions.extend((
            open_orders_available_gate(True),
            open_orders_gate(len(open_orders),cfg["execution"]["max_open_orders"]),
        ))
    combined=combine(*decisions)

    if not range_approved:
        combined=combine(combined,type(combined)(False,(f"RANGE:{range_reason}",)))
    if not grid_allowed:
        combined=combine(combined,type(combined)(False,(f"GRID:{grid_reason}",)))
    if market_intelligence_decision is not None and not market_intelligence_decision.allowed:
        reasons_str = "|".join(r.value for r in market_intelligence_decision.reasons)
        combined=combine(combined,type(combined)(False,(f"MARKET_INTELLIGENCE:GRID_BLOCKED:{reasons_str}",)))
    if adaptive_plan is not None and adaptive_plan.decision == PlanDecision.GRID_BLOCKED:
        reasons_str = "|".join(r.value for r in adaptive_plan.reasons)
        combined=combine(combined,type(combined)(False,(f"ADAPTIVE_PLANNER:GRID_BLOCKED:{reasons_str}",)))

    record_risk_event(db_path,combined.allowed,combined.reason,{
        "symbol":symbol,"price":str(current_price),
        "range":[str(lower),str(effective_upper)],
        "range_quality":range_quality,
        "market_regime": market_intelligence_decision.regime.value if market_intelligence_decision else None,
        "market_intelligence_status": market_intelligence_decision.status.value if market_intelligence_decision else None,
        "market_intelligence_reasons": [r.value for r in market_intelligence_decision.reasons] if market_intelligence_decision else None,
        "range_quality_score": str(market_intelligence_decision.range_quality_score) if market_intelligence_decision else None,
        "grid_levels":len(levels),
        "min_net_pct":str(plan_validation.min_net_pct if plan_validation else Decimal("0")),
        "grid_reason":grid_reason,
        "fee_source":fee_source,
        "current_equity":str(account_risk.current_equity) if account_risk else None,
        "reference_equity":str(account_risk.reference_equity) if account_risk else None,
        "drawdown_pct":str(account_risk.drawdown_pct) if account_risk else None,
        "base_inventory":str(account_risk.base_inventory) if account_risk else None,
        "account_error":account_error,
        "open_orders_count":len(open_orders) if open_orders is not None else None,
        "open_orders_status":"VERIFIED" if open_orders is not None else "UNAVAILABLE",
        "open_orders_error":open_orders_error,
        "adaptive_plan_decision": adaptive_plan.decision.value if adaptive_plan is not None else None,
        "adaptive_plan_reasons": [r.value for r in adaptive_plan.reasons] if adaptive_plan is not None else None,
    })

    set_state(db_path,"last_symbol",symbol)
    set_state(db_path,"last_price",str(current_price))
    set_state(db_path,"last_range",{"lower":str(lower),"upper":str(effective_upper)})
    set_state(db_path,"last_risk_decision",{"allowed":combined.allowed,"reason":combined.reason})
    if market_intelligence_decision is not None:
        set_state(db_path,"last_market_intelligence",{
            "status": market_intelligence_decision.status.value,
            "allowed": market_intelligence_decision.allowed,
            "reasons": [r.value for r in market_intelligence_decision.reasons],
            "regime": market_intelligence_decision.regime.value,
            "range_quality_score": str(market_intelligence_decision.range_quality_score),
            "diagnostics": market_intelligence_decision.diagnostics,
        })
    if adaptive_plan is not None:
        set_state(db_path,"last_adaptive_plan",{
            "plan_id": adaptive_plan.plan_id,
            "decision": adaptive_plan.decision.value,
            "reasons": [r.value for r in adaptive_plan.reasons],
            "regime": adaptive_plan.regime.value,
            "range_quality_score": str(adaptive_plan.range_quality_score),
            "candidate_lower": str(adaptive_plan.candidate_lower),
            "candidate_upper": str(adaptive_plan.candidate_upper),
            "grid_step": str(adaptive_plan.grid_step),
            "grid_count": adaptive_plan.grid_count,
            "estimated_net_profit_per_grid": str(adaptive_plan.estimated_net_profit_per_grid),
        })
    if account_risk:
        set_state(db_path,"last_account_risk",{
            "current_equity":str(account_risk.current_equity),
            "reference_equity":str(account_risk.reference_equity),
            "drawdown_pct":str(account_risk.drawdown_pct),
            "base_inventory":str(account_risk.base_inventory),
            "inventory_pct":str(account_risk.inventory_pct),
        })
    # Informational only: this state is never read as current exchange truth.
    set_state(db_path,"last_open_order_reconciliation",{
        "count":len(open_orders) if open_orders is not None else None,
        "status":"VERIFIED" if open_orders is not None else "UNAVAILABLE",
        "error":open_orders_error,
    })

    cells=validation.cells if validation else 0
    min_net=plan_validation.min_net_pct if plan_validation else Decimal("0")

    logger.info("=== Adaptive Grid v3.2.1 Safety Foundation ===")
    logger.info("Mode=%s dry_run=%s symbol=%s",mode,cfg["environment"]["dry_run"],symbol)
    logger.info("Price=%s Range=%s -> %s Quality=%.2f Reason=%s Position=%.2f",
                current_price,lower,effective_upper,range_quality,range_reason,position_in_range)
    logger.info("Fees maker=%s taker=%s source=%s | Grid step=%.3f%% cells=%d effective_upper=%s",
                fees.maker,fees.taker,fee_source,float(cfg["grid"]["step_pct"])*100,cells,effective_upper)
    logger.info("Indicators ADX=%.2f ATR=%.3f%% BB=%.3f%% Vol=%.2fx RSI=%.2f",
                float(last["adx"]),float(last["atr_pct"])*100,float(last["bb_width"])*100,
                float(last["volume_ratio"]),float(last["rsi"]))
    logger.info("Net/grid=%.4f%% class=%s grid=%s market/risk=%s",
                float(min_net)*100,
                profit_class(min_net,cfg["grid"]["hard_min_net_pct"],cfg["grid"]["preferred_net_max_pct"]),
                grid_reason,combined.reason)
    if adaptive_plan is not None:
        logger.info("Adaptive planner decision=%s reasons=%s grid_count=%d net_pct=%.4f%%",
                    adaptive_plan.decision.value,
                    [r.value for r in adaptive_plan.reasons],
                    adaptive_plan.grid_count,
                    float(adaptive_plan.estimated_net_profit_per_grid)*100)
    logger.info("Symbol rules tick=%s step=%s minQty=%s minNotional=%s",
                rules.tick_size,rules.step_size,rules.min_qty,rules.min_notional)
    if account_risk:
        logger.info("Account equity=%s %s reference=%s drawdown=%.4f%% base_inventory=%s %s",
                    account_risk.current_equity,rules.quote_asset,account_risk.reference_equity,
                    float(account_risk.drawdown_pct)*100,account_risk.base_inventory,rules.base_asset)
    else:
        logger.error("Account risk state unavailable: %s",account_error)
    if open_orders is None:
        logger.error("Open-order reconciliation unavailable: %s",open_orders_error)
    else:
        logger.info("Open orders verified: count=%d",len(open_orders))

    # FIX 4B: paper execution is routed through the authoritative orchestrator
    # (PaperSession.run_cycle).  The orchestrator owns a single atomic cycle
    # transaction that enforces the lifecycle-integrity gate, the
    # inventory-allocation gate, the post-quantization profit gate, order
    # submission, deterministic fills and recovery.  This removes the old
    # non-transactional, per-order ``_submit_paper_orders`` loop (F-1/F-2).
    cycle_result = None
    # FIX 4B: run the paper cycle through the orchestrator only when main's
    # planner allows a NEW grid (allow_new_orders is True solely on a fresh
    # GRID_ALLOWED; keep/reconfig/cooldown/blocked leave the active plan
    # untouched — the original ``submit_new`` semantics).  This keeps the
    # orchestrator's lifecycle DB in sync with main's decision and skips the
    # submission cycle on keep/reconfig so no new orders are placed.
    if (
        combined.allowed
        and plan_validation is not None
        and allow_new_orders
    ):
        cycle_candle_index = int(get_state(db_path, "paper_cycle_index") or "0") + 1
        set_state(db_path, "paper_cycle_index", str(cycle_candle_index))
        cycle_regime = (
            market_intelligence_decision.regime
            if market_intelligence_decision is not None
            else (adaptive_plan.regime if adaptive_plan is not None else MarketRegime.RANGE)
        )
        cycle_quality = (
            market_intelligence_decision.range_quality_score
            if market_intelligence_decision is not None
            else (adaptive_plan.range_quality_score if adaptive_plan is not None else Decimal("80"))
        )
        cycle_result = _run_paper_cycle(
            db_path,
            cfg,
            symbol,
            rules,
            current_price=current_price,
            kline_df=df,
            lower=lower,
            upper=upper,
            risk_decision=combined,
            regime=cycle_regime,
            range_quality_score=cycle_quality,
            candle_index=cycle_candle_index,
            active_plan=active_plan,
        )
        set_state(db_path, "last_paper_orders", {
            "count": cycle_result.orders_submitted,
            "states": {"OPEN": cycle_result.orders_submitted}
            if cycle_result.orders_submitted else {},
            "cycle_id": cycle_result.cycle_id,
            "plan_decision": (
                cycle_result.plan_decision.value
                if cycle_result.plan_decision else None
            ),
            "lifecycle_transition": cycle_result.lifecycle_transition,
            "orders_submitted": cycle_result.orders_submitted,
            "orders_skipped": cycle_result.orders_skipped,
            "fills_applied": cycle_result.fills_applied,
            "recovery_healthy": cycle_result.recovery_healthy,
            "is_idempotent": cycle_result.is_idempotent,
            "success": cycle_result.success,
            "blocked_reason": cycle_result.blocked_reason,
            "error": cycle_result.error,
        })

    if not combined.allowed:
        logger.warning("ORDER PLAN BLOCKED: %s",combined.reason)
    elif cycle_result is not None:
        logger.info(
            "PAPER CYCLE PASS: submitted=%d skipped=%d fills=%d plan=%s "
            "transition=%s recovery_healthy=%s success=%s",
            cycle_result.orders_submitted,
            cycle_result.orders_skipped,
            cycle_result.fills_applied,
            cycle_result.plan_decision.value
            if cycle_result.plan_decision else None,
            cycle_result.lifecycle_transition,
            cycle_result.recovery_healthy,
            cycle_result.success,
        )
        if cycle_result.blocked_reason:
            logger.error("PAPER CYCLE BLOCKED: %s", cycle_result.blocked_reason)
        if cycle_result.error:
            logger.error("PAPER CYCLE ROLLED BACK (no mutation persisted): %s",
                         cycle_result.error)

    print("\nResult:")
    print(f"  Risk decision : {'PASS' if combined.allowed else 'BLOCK'}")
    print(f"  Reason        : {combined.reason}")
    if market_intelligence_decision is not None:
        print(f"  Market regime : {market_intelligence_decision.regime.value}")
        print(f"  Grid allowed  : {'YES' if market_intelligence_decision.allowed else 'NO'}")
        if not market_intelligence_decision.allowed:
            print(f"  Block reasons : {[r.value for r in market_intelligence_decision.reasons]}")
    if adaptive_plan is not None:
        print(f"  Adaptive plan : {adaptive_plan.decision.value}")
        print(f"  Plan grid     : cells={adaptive_plan.grid_count} range={adaptive_plan.candidate_lower} -> {adaptive_plan.candidate_upper}")
        if adaptive_plan.reasons:
            print(f"  Plan block    : {[r.value for r in adaptive_plan.reasons]}")
    print(f"  Price         : {current_price}")
    print(f"  Range         : {lower} -> {effective_upper}")
    print(f"  Grid cells    : {cells}")
    print(f"  Net/grid      : {min_net*100:.4f}%")
    print(f"  Range quality : {range_quality:.2f}/100")
    print(f"  Fee source    : {fee_source}")
    if account_risk:
        print(f"  Equity        : {account_risk.current_equity} {rules.quote_asset}")
        print(f"  Drawdown      : {account_risk.drawdown_pct*100:.4f}%")
    else:
        print(f"  Account state : BLOCKED ({account_error})")
    if open_orders is None:
        print(f"  Open orders   : UNAVAILABLE ({open_orders_error})")
    else:
        print(f"  Open orders   : VERIFIED ({len(open_orders)})")
    print("  Execution     : DRY RUN, no order placement")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
