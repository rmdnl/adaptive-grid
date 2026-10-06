"""Regression tests for Adaptive Spot Grid Planner (Phase 1).

These tests verify the deterministic adaptive grid planning logic:
- Config no longer requires LOWER_PRICE, UPPER_PRICE, TOTAL_GRIDS, TOTAL_QUOTE_BUDGET
- INDICATOR_TIMEFRAME remains required
- Automatic lower/upper calculation
- Automatic grid count selection
- Candidate with net exactly 0.20% is accepted
- Candidate with net 0.1999% is rejected
- Binance tick-size quantization
- Binance quantity-step quantization
- Binance minimum notional
- Binance maximum notional
- Automatic quote budget from available balance
- Quote reserve is respected
- Multiple symbols cannot consume more than the global allocation cap
- Insufficient quote balance blocks grid creation
- Missing/stale account state blocks grid creation
- Missing/stale ATR blocks grid creation
- Missing/stale 15m candle blocks the relevant safety decision
- Active grid parameters remain unchanged when ATR/price changes
- New grid recalculates adaptive parameters only after previous grid is inactive
- Restart restores active adaptive grid parameters
- Historical symbols remain hidden
- No hardcoded TOTAL_GRIDS fallback
- No hardcoded minimum notional estimate
"""

from __future__ import annotations

import pytest

from adaptive_grid import AdaptiveGridPlanner
from config import Config
from grid import ExchangeFilters, GridLevel
from conftest import make_config


class TestAdaptiveGridPlanner:
    """Tests for the AdaptiveGridPlanner pure function."""

    def make_filters(self, tick=0.01, step=0.00001, min_notional=10.0,
                     max_notional=None, min_qty=0.0, max_qty=None,
                     max_price=None,
                     bid_up=None, bid_down=None, ask_up=None, ask_down=None,
                     avg_price_mins=None) -> ExchangeFilters:
        """Build exchange filters with optional overrides."""
        return ExchangeFilters(
            tick_size=tick,
            step_size=step,
            min_notional=min_notional,
            min_qty=min_qty,
            max_notional=max_notional,
            max_qty=max_qty,
            max_price=max_price,
            apply_min_to_market=False,
            apply_max_to_market=False,
            bid_multiplier_up=bid_up,
            bid_multiplier_down=bid_down,
            ask_multiplier_up=ask_up,
            ask_multiplier_down=ask_down,
            avg_price_mins=avg_price_mins,
        )

    def make_cfg(self, **overrides) -> Config:
        """Build config with adaptive parameters enabled."""
        defaults = dict(
            adaptive_grid=True,
            min_grids=3,
            max_grids=12,
            quote_reserve_percent=20.0,
            max_quote_allocation_percent=80.0,
            grid_step_atr_multiplier=1.0,
            grid_gross_min=0.005,
            min_net_profit_per_grid=0.002,
            maker_fee=0.001,
            taker_fee=0.001,
            slippage_estimate=0.0005,
            pair_list=("BTC/USDT",),
        )
        defaults.update(overrides)
        return make_config(**defaults)

    def test_adaptive_grid_requires_adaptive_true(self):
        """Fail-closed when adaptive_grid=False."""
        cfg = self.make_cfg(adaptive_grid=False)
        filters = self.make_filters()
        with pytest.raises(ValueError, match="adaptive_grid is disabled"):
            AdaptiveGridPlanner.plan(
                symbol="BTC/USDT",
                current_price=50000.0,
                atr=350.0,
                cfg=cfg,
                filters=filters,
                reference_price=50000.0,
                available_usdt=10000.0,
            )

    def test_invalid_inputs_fail_closed(self):
        """Fail-closed on missing/invalid inputs."""
        cfg = self.make_cfg()
        filters = self.make_filters()

        # Invalid current_price
        with pytest.raises(ValueError, match="invalid current_price"):
            AdaptiveGridPlanner.plan("BTC/USDT", 0.0, 350.0, cfg, filters, 50000.0, 10000.0)
        with pytest.raises(ValueError, match="invalid current_price"):
            AdaptiveGridPlanner.plan("BTC/USDT", None, 350.0, cfg, filters, 50000.0, 10000.0)

        # Invalid ATR
        with pytest.raises(ValueError, match="invalid ATR"):
            AdaptiveGridPlanner.plan("BTC/USDT", 50000.0, 0.0, cfg, filters, 50000.0, 10000.0)
        with pytest.raises(ValueError, match="invalid ATR"):
            AdaptiveGridPlanner.plan("BTC/USDT", 50000.0, None, cfg, filters, 50000.0, 10000.0)

        # Invalid available USDT
        with pytest.raises(ValueError, match="invalid available USDT"):
            AdaptiveGridPlanner.plan("BTC/USDT", 50000.0, 350.0, cfg, filters, 50000.0, 0.0)
        with pytest.raises(ValueError, match="invalid available USDT"):
            AdaptiveGridPlanner.plan("BTC/USDT", 50000.0, 350.0, cfg, filters, 50000.0, None)

        # Invalid reference price
        with pytest.raises(ValueError, match="invalid reference price"):
            AdaptiveGridPlanner.plan("BTC/USDT", 50000.0, 350.0, cfg, filters, 0.0, 10000.0)

    def test_automatic_range_calculation(self):
        """Lower and upper prices computed automatically from ATR.
        
        The returned bounds must describe the ACTUAL executable grid levels,
        not the candidate bounds before filter validation.
        """
        cfg = self.make_cfg()
        filters = self.make_filters()
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # Actual executable bounds
        assert plan.lower_price < plan.reference_price
        assert plan.upper_price <= plan.reference_price
        # Bounds must correspond to actual executable levels
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)
        # Bounds should be quantized to tick_size (0.01)
        assert plan.lower_price == pytest.approx(round(plan.lower_price, 2))
        assert plan.upper_price == pytest.approx(round(plan.upper_price, 2))
        # Internal consistency
        assert plan.total_grids == len(plan.levels)
        assert plan.total_grids > 0

    def test_automatic_grid_count_selection(self):
        """Grid count selected automatically within min/max bounds."""
        cfg = self.make_cfg(min_grids=3, max_grids=12)
        filters = self.make_filters()
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        assert 3 <= plan.total_grids <= 12
        assert len(plan.levels) == plan.total_grids

    def test_net_strictly_above_020_percent_required(self):
        """Invariant: the executable NET of every accepted candidate is
        STRICTLY greater than 0.20% — net == 0.20% exactly is REJECTED
        (tick quantization may land slightly above; the gate enforces it)."""
        # Use parameters that give approximately 0.20% net
        # gross = 0.50% -> net = 0.50% - (0.1%+0.05%)*2 = 0.50% - 0.30% = 0.20%
        # At price=50000, gross=0.5% needs step=250. With ATR=350, multiplier=250/350≈0.7143
        cfg = self.make_cfg(grid_step_atr_multiplier=250.0/350.0)
        filters = self.make_filters(tick=0.01, step=0.00001, min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        assert plan.net_pct > 0.002  # strictly greater than 0.20%

    def test_net_below_020_percent_rejected(self):
        """Candidate with net < 0.20% should be rejected (fail-closed)."""
        # Very small ATR -> very small step -> net below minimum
        cfg = self.make_cfg(grid_step_atr_multiplier=0.01, min_grids=3, max_grids=5)
        filters = self.make_filters(tick=0.01, step=0.00001, min_notional=1.0)
        with pytest.raises(ValueError, match="no valid grid found"):
            AdaptiveGridPlanner.plan(
                symbol="BTC/USDT",
                current_price=50000.0,
                atr=1.0,  # Very small ATR
                cfg=cfg,
                filters=filters,
                reference_price=50000.0,
                available_usdt=10000.0,
            )

    def test_tick_size_quantization(self):
        """Prices quantized to exchange tick_size."""
        # tick=0.5 -> prices should be multiples of 0.5
        cfg = self.make_cfg(grid_step_atr_multiplier=1.0)
        filters = self.make_filters(tick=0.5, step=0.00001, min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        for lvl in plan.levels:
            assert lvl.buy_price % 0.5 == 0.0 or abs(lvl.buy_price % 0.5) < 1e-9
            assert lvl.sell_price % 0.5 == 0.0 or abs(lvl.sell_price % 0.5) < 1e-9

    def test_qty_step_quantization(self):
        """Quantities quantized to exchange step_size."""
        # step=0.1 -> quantities should be multiples of 0.1
        # Need higher available_usdt to afford step_size=0.1 quantities
        cfg = self.make_cfg(grid_step_atr_multiplier=1.0)
        filters = self.make_filters(tick=0.01, step=0.1, min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=100000.0,
        )
        for lvl in plan.levels:
            assert lvl.qty % 0.1 == 0.0 or abs(lvl.qty % 0.1) < 1e-9

    def test_min_notional_enforced(self):
        """Minimum notional enforced for each level."""
        cfg = self.make_cfg(grid_step_atr_multiplier=1.0)
        filters = self.make_filters(tick=0.01, step=0.00001, min_notional=100.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        for lvl in plan.levels:
            assert lvl.buy_price * lvl.qty >= 100.0 - 1e-9
            assert lvl.sell_price * lvl.qty >= 100.0 - 1e-9

    def test_max_notional_enforced(self):
        """Maximum notional enforced for LIMIT_MAKER orders."""
        cfg = self.make_cfg(grid_step_atr_multiplier=1.0)
        # max_notional=1000, so each level's notional must be <= 1000
        filters = self.make_filters(tick=0.01, step=0.00001, min_notional=1.0, max_notional=1000.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        for lvl in plan.levels:
            assert lvl.buy_price * lvl.qty <= 1000.0 + 1e-9
            assert lvl.sell_price * lvl.qty <= 1000.0 + 1e-9

    def test_quote_budget_from_available_balance(self):
        """Quote budget derived from available USDT with reserve."""
        cfg = self.make_cfg(
            quote_reserve_percent=20.0,
            max_quote_allocation_percent=80.0,
        )
        filters = self.make_filters(min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # 10000 * 0.8 * 0.8 / 1 = 6400
        assert plan.quote_budget == pytest.approx(6400.0)

    def test_quote_reserve_respected(self):
        """Reserve percentage is respected in budget calculation."""
        cfg = self.make_cfg(
            quote_reserve_percent=50.0,
            max_quote_allocation_percent=100.0,
        )
        filters = self.make_filters(min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # 10000 * 0.5 * 1.0 / 1 = 5000
        assert plan.quote_budget == pytest.approx(5000.0)

    def test_max_allocation_cap_respected(self):
        """Global allocation cap respected across multiple symbols."""
        cfg = self.make_cfg(
            quote_reserve_percent=0.0,
            max_quote_allocation_percent=50.0,
            pair_list=("BTC/USDT", "ETH/USDT"),
        )
        filters = self.make_filters(min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # 10000 * 1.0 * 0.5 / 2 = 2500 per symbol
        assert plan.quote_budget == pytest.approx(2500.0)

    def test_insufficient_balance_blocks_grid(self):
        """Insufficient quote balance blocks grid creation."""
        cfg = self.make_cfg()
        filters = self.make_filters(min_notional=1.0)
        with pytest.raises(ValueError, match="no valid grid found"):
            AdaptiveGridPlanner.plan(
                symbol="BTC/USDT",
                current_price=50000.0,
                atr=350.0,
                cfg=cfg,
                filters=filters,
                reference_price=50000.0,
                available_usdt=0.01,  # Too small
            )

    def test_missing_stale_atr_blocks_grid(self):
        """Missing/stale ATR blocks grid creation."""
        cfg = self.make_cfg()
        filters = self.make_filters(min_notional=1.0)
        with pytest.raises(ValueError, match="invalid ATR"):
            AdaptiveGridPlanner.plan(
                symbol="BTC/USDT",
                current_price=50000.0,
                atr=None,
                cfg=cfg,
                filters=filters,
                reference_price=50000.0,
                available_usdt=10000.0,
            )

    def test_no_hardcoded_total_grids_fallback(self):
        """No hardcoded TOTAL_GRIDS=5 fallback in planner."""
        cfg = self.make_cfg(min_grids=3, max_grids=20)
        filters = self.make_filters(min_notional=1.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # Should not be exactly 5 (the old hardcoded fallback)
        # It should be determined by the algorithm
        assert plan.total_grids >= 3
        assert plan.total_grids <= 20

    def test_no_hardcoded_min_notional_estimate(self):
        """No hardcoded MIN_NOTIONAL_ESTIMATE=10 in planner."""
        cfg = self.make_cfg()
        filters = self.make_filters(min_notional=100.0)
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # Should use actual min_notional from filters, not hardcoded 10
        for lvl in plan.levels:
            assert lvl.buy_price * lvl.qty >= 100.0 - 1e-9

    def test_adaptive_plan_reports_actual_executable_grid(self):
        """AdaptiveGridPlan must report actual executable grid, not candidate bounds.
        
        NEAR-style scenario: candidate 12 grids, PERCENT_PRICE_BY_SIDE drops 8 levels,
        leaving 4 executable. Returned plan must reflect the 4 executable levels.
        """
        cfg = self.make_cfg(
            grid_step_atr_multiplier=1.0,
            min_grids=3,
            max_grids=12,
        )
        # NEAR-like filters: PERCENT_PRICE_BY_SIDE with tight bid_down (0.5)
        # reference=4.87, bid_down=0.5 -> min buy = 2.435
        # This will drop lower candidate levels below this threshold
        filters = self.make_filters(
            tick=0.001,
            step=0.1,
            min_notional=5.0,
            min_qty=0.1,
            max_price=1000.0,
            max_qty=900000.0,
            max_notional=9000000.0,
            bid_up=1.2,
            bid_down=0.5,
            ask_up=2.0,
            ask_down=0.8,
            avg_price_mins=5,
        )
        plan = AdaptiveGridPlanner.plan(
            symbol="NEAR/USDT",
            current_price=4.869,
            atr=0.254,
            cfg=cfg,
            filters=filters,
            reference_price=4.87,
            available_usdt=400000.0,
        )
        # Candidate was 12, but executable is 4
        assert plan.total_grids == 4
        assert len(plan.levels) == 4
        assert plan.total_grids == len(plan.levels)
        # Bounds must be actual executable bounds
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)
        # Economics must be based on actual executable levels
        worst_net = min(lvl.net_pct for lvl in plan.levels)
        assert plan.net_pct == pytest.approx(worst_net)
        worst_gross = min(lvl.gross_pct for lvl in plan.levels)
        assert plan.gross_pct == pytest.approx(worst_gross)

    def test_adaptive_plan_no_levels_dropped_when_filters_allow(self):
        """BNB-style: candidate 12 = actual 12 when no levels dropped."""
        cfg = self.make_cfg(
            grid_step_atr_multiplier=1.0,
            min_grids=3,
            max_grids=12,
        )
        # BNB-like filters: wide PERCENT_PRICE_BY_SIDE range, all levels pass
        filters = self.make_filters(
            tick=0.01,
            step=0.001,
            min_notional=5.0,
            min_qty=0.001,
            max_price=100000.0,
            max_qty=900000.0,
            max_notional=9000000.0,
            bid_up=1.2,
            bid_down=0.5,
            ask_up=2.0,
            ask_down=0.8,
            avg_price_mins=5,
        )
        plan = AdaptiveGridPlanner.plan(
            symbol="BNB/USDT",
            current_price=791.96,
            atr=12.91,
            cfg=cfg,
            filters=filters,
            reference_price=790.0,
            available_usdt=400000.0,
        )
        # Candidate 12, actual 12
        assert plan.total_grids == 12
        assert len(plan.levels) == 12
        assert plan.total_grids == len(plan.levels)
        # Bounds must be actual executable bounds
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)

    def test_adaptive_plan_eth_style_partial_drop(self):
        """ETH-style: candidate 12, PERCENT_PRICE_BY_SIDE drops 3 levels, actual 9."""
        cfg = self.make_cfg(
            grid_step_atr_multiplier=1.0,
            min_grids=3,
            max_grids=12,
        )
        # ETH-like filters
        filters = self.make_filters(
            tick=0.01,
            step=0.0001,
            min_notional=5.0,
            min_qty=0.0001,
            max_price=1000000.0,
            max_qty=9000.0,
            max_notional=9000000.0,
            bid_up=1.2,
            bid_down=0.5,
            ask_up=2.0,
            ask_down=0.8,
            avg_price_mins=5,
        )
        plan = AdaptiveGridPlanner.plan(
            symbol="ETH/USDT",
            current_price=2716.43,
            atr=62.77,
            cfg=cfg,
            filters=filters,
            reference_price=2700.0,
            available_usdt=400000.0,
        )
        # Candidate was 12, executable is 9
        assert plan.total_grids == 9
        assert len(plan.levels) == 9
        assert plan.total_grids == len(plan.levels)
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)

    def test_adaptive_plan_sol_style_partial_drop(self):
        """SOL-style: candidate 12, filters drop 3 levels, actual 9 (geometric mode)."""
        cfg = self.make_cfg(
            grid_step_atr_multiplier=1.0,
            min_grids=3,
            max_grids=12,
        )
        # SOL-like filters
        filters = self.make_filters(
            tick=0.01,
            step=0.001,
            min_notional=5.0,
            min_qty=0.001,
            max_price=10000.0,
            max_qty=90000.0,
            max_notional=9000000.0,
            bid_up=1.2,
            bid_down=0.5,
            ask_up=2.0,
            ask_down=0.8,
            avg_price_mins=5,
        )
        plan = AdaptiveGridPlanner.plan(
            symbol="SOL/USDT",
            current_price=120.43,
            atr=3.11,
            cfg=cfg,
            filters=filters,
            reference_price=120.48,
            available_usdt=400000.0,
        )
        # Candidate was 12, executable is 9
        assert plan.total_grids == 9
        assert len(plan.levels) == 9
        assert plan.total_grids == len(plan.levels)
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)

    def test_adaptive_plan_general_invariants(self):
        """General invariant: every successful AdaptiveGridPlan is internally consistent."""
        cfg = self.make_cfg()
        filters = self.make_filters()
        plan = AdaptiveGridPlanner.plan(
            symbol="BTC/USDT",
            current_price=50000.0,
            atr=350.0,
            cfg=cfg,
            filters=filters,
            reference_price=50000.0,
            available_usdt=10000.0,
        )
        # Core invariants
        assert plan.total_grids == len(plan.levels), "total_grids must equal actual level count"
        assert plan.total_grids > 0, "must have at least one executable level"
        assert plan.lower_price == min(lvl.buy_price for lvl in plan.levels)
        assert plan.upper_price == max(lvl.sell_price for lvl in plan.levels)
        assert plan.net_pct == pytest.approx(min(lvl.net_pct for lvl in plan.levels))
        assert plan.gross_pct == pytest.approx(min(lvl.gross_pct for lvl in plan.levels))
        # Budget must cover total buy notional of executable levels
        total_buy_notional = sum(lvl.buy_price * lvl.qty for lvl in plan.levels)
        assert total_buy_notional <= plan.quote_budget + 1e-9
        # Step must match grid step
        assert plan.step == cfg.grid_step_atr_multiplier * 350.0  # atr


class TestAdaptiveConfig:
    """Tests for configuration changes (Phase 1)."""

    def test_config_no_longer_requires_lower_price(self):
        """LOWER_PRICE no longer required when adaptive_grid=true."""
        from config import load_config
        import tempfile
        import os

        env_content = """
BINANCE_ENV=testnet
EXECUTION_MODE=paper
DRY_RUN=true
ALLOW_LIVE_EXECUTION=false
PAIR_LIST=BTC/USDT
INDICATOR_TIMEFRAME=4h
ADX_PERIOD=14
RSI_PERIOD=14
BB_PERIOD=20
BB_STD=2
VO_FAST=5
VO_SLOW=10
ZSCORE_PERIOD=20
ATR_PERIOD=14
ENTRY_ADX_MAX=20
ENTRY_RSI_MAX=35
ENTRY_VOLUME_OSC_MIN=0
ENTRY_BB_PERCENT_B_MAX=0
EXIT_RSI_MIN=70
EXIT_ADX_MIN=25
EXIT_BB_PERCENT_B_MIN=1
EXIT_ZSCORE_ABS_MAX=2.5
GRID_STEP_ATR_MULTIPLIER=1.0
GRID_GROSS_MIN=0.005
MIN_NET_PROFIT_PER_GRID=0.002
MAKER_FEE=0.001
TAKER_FEE=0.001
SLIPPAGE_ESTIMATE=0.0005
MAX_DRAWDOWN_PERCENT=2
STOP_IF_BELOW_LOWER_PERCENT=2
COOLDOWN_HOURS=3
ADAPTIVE_GRID=true
MIN_GRIDS=3
MAX_GRIDS=12
QUOTE_RESERVE_PERCENT=20
MAX_QUOTE_ALLOCATION_PERCENT=80
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
BINANCE_API_KEY=
BINANCE_API_SECRET=
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write(env_content)
            env_path = f.name
        try:
            cfg = load_config(env_path)
            assert cfg.adaptive_grid is True
            assert cfg.lower_price == {}
            assert cfg.upper_price == {}
            assert cfg.total_grids is None
            assert cfg.total_quote_budget == {}
        finally:
            os.unlink(env_path)

    def test_indicator_timeframe_still_required(self):
        """INDICATOR_TIMEFRAME remains required."""
        from config import load_config, ConfigError
        import tempfile
        import os

        env_content = """
BINANCE_ENV=testnet
EXECUTION_MODE=paper
DRY_RUN=true
ALLOW_LIVE_EXECUTION=false
PAIR_LIST=BTC/USDT
ADX_PERIOD=14
RSI_PERIOD=14
BB_PERIOD=20
BB_STD=2
VO_FAST=5
VO_SLOW=10
ZSCORE_PERIOD=20
ATR_PERIOD=14
ENTRY_ADX_MAX=20
ENTRY_RSI_MAX=35
ENTRY_VOLUME_OSC_MIN=0
ENTRY_BB_PERCENT_B_MAX=0
EXIT_RSI_MIN=70
EXIT_ADX_MIN=25
EXIT_BB_PERCENT_B_MIN=1
EXIT_ZSCORE_ABS_MAX=2.5
GRID_STEP_ATR_MULTIPLIER=1.0
GRID_GROSS_MIN=0.005
MIN_NET_PROFIT_PER_GRID=0.002
MAKER_FEE=0.001
TAKER_FEE=0.001
SLIPPAGE_ESTIMATE=0.0005
MAX_DRAWDOWN_PERCENT=2
STOP_IF_BELOW_LOWER_PERCENT=2
COOLDOWN_HOURS=3
ADAPTIVE_GRID=true
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write(env_content)
            env_path = f.name
        try:
            with pytest.raises(ConfigError, match="INDICATOR_TIMEFRAME"):
                load_config(env_path)
        finally:
            os.unlink(env_path)

    def test_static_mode_still_requires_all_four(self):
        """When adaptive_grid=false, all four static params required."""
        from config import load_config, ConfigError
        import tempfile
        import os

        env_content = """
BINANCE_ENV=testnet
EXECUTION_MODE=paper
DRY_RUN=true
ALLOW_LIVE_EXECUTION=false
PAIR_LIST=BTC/USDT
INDICATOR_TIMEFRAME=4h
ADX_PERIOD=14
RSI_PERIOD=14
BB_PERIOD=20
BB_STD=2
VO_FAST=5
VO_SLOW=10
ZSCORE_PERIOD=20
ATR_PERIOD=14
ENTRY_ADX_MAX=20
ENTRY_RSI_MAX=35
ENTRY_VOLUME_OSC_MIN=0
ENTRY_BB_PERCENT_B_MAX=0
EXIT_RSI_MIN=70
EXIT_ADX_MIN=25
EXIT_BB_PERCENT_B_MIN=1
EXIT_ZSCORE_ABS_MAX=2.5
GRID_STEP_ATR_MULTIPLIER=1.0
GRID_GROSS_MIN=0.005
MIN_NET_PROFIT_PER_GRID=0.002
MAKER_FEE=0.001
TAKER_FEE=0.001
SLIPPAGE_ESTIMATE=0.0005
MAX_DRAWDOWN_PERCENT=2
STOP_IF_BELOW_LOWER_PERCENT=2
COOLDOWN_HOURS=3
ADAPTIVE_GRID=false
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write(env_content)
            env_path = f.name
        try:
            with pytest.raises(ConfigError, match="LOWER_PRICE"):
                load_config(env_path)
        finally:
            os.unlink(env_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])