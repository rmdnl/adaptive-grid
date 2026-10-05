# Adaptive Spot Grid Implementation Plan

## Architecture Overview
The existing codebase uses a rigid static grid model where LOWER_PRICE, UPPER_PRICE, TOTAL_GRIDS, and TOTAL_QUOTE_BUDGET are mandatory .env values. The goal is to compute these automatically at runtime using ATR-based range construction while preserving all safety invariants.

## Phase 1: Configuration Changes (config.py)

### Remove mandatory requirements:
- LOWER_PRICE, UPPER_PRICE, TOTAL_GRIDS, TOTAL_QUOTE_BUDGET: Make optional (no ConfigError when absent)

### Add adaptive policy parameters (with conservative defaults):
- `ADAPTIVE_GRID=true` (boolean, default true)
- `MIN_GRIDS=3` (minimum candidate grid count)
- `MAX_GRIDS=12` (maximum candidate grid count)
- `QUOTE_RESERVE_PERCENT=20` (reserve % of available USDT)
- `MAX_QUOTE_ALLOCATION_PERCENT=80` (max % of available USDT to allocate across all symbols)

### Keep required:
- `INDICATOR_TIMEFRAME` (remains mandatory, drives snapshot timeframe)

### Config object additions:
```python
adaptive_grid: bool
min_grids: int
max_grids: int
quote_reserve_percent: float
max_quote_allocation_percent: float
```

## Phase 2: Adaptive Grid Planner (new: adaptive_grid.py)

Pure deterministic module, no exchange I/O:
```python
@dataclass(frozen=True)
class AdaptiveGridPlan:
    lower_price: float
    upper_price: float
    total_grids: int
    quote_budget: float
    step: float
    reference_price: float
    levels: List[GridLevel]  # Reuse GridLevel from grid.py

class AdaptiveGridPlanner:
    @staticmethod
    def plan(symbol: str, current_price: float, atr: float, cfg: Config, 
             filters: ExchangeFilters, available_usdt: float) -> AdaptiveGridPlan:
        """
        Deterministic algorithm:
        1. Compute grid step = atr * GRID_STEP_ATR_MULTIPLIER
        2. Build symmetric ATR-based range: ±N * step around current_price
           (N = max_grids / 2, ensuring upper ≥ current ≥ lower)
        3. Quantize bounds to tick_size
        4. For candidate_count in range(MIN_GRIDS, MAX_GRIDS + 1):
           a. Build grid using existing grid.build_grid() with candidate bounds/count
           b. Validate executable economics (net >= MIN_NET_PROFIT_PER_GRID)
           c. Validate against Binance filters
           d. Check quote budget feasibility
        5. Select highest passing candidate_count
        6. Fail-closed if no candidate passes
        """
```

## Phase 3: State Persistence (state.py)

Extend `SymbolState` dataclass with adaptive fields:
```python
adaptive_lower_price: Optional[float] = None
adaptive_upper_price: Optional[float] = None
adaptive_total_grids: Optional[int] = None
adaptive_quote_budget: Optional[float] = None
adaptive_grid_step: Optional[float] = None
adaptive_reference_price: Optional[float] = None
adaptive_timeframe: Optional[str] = None
```

Database schema migration to v4: Add columns to `symbols` table.

When grid becomes ACTIVE, persist adaptive values alongside existing grid_mode/grid_step/grid_lower.

## Phase 4: Bot Integration (bot.py)

### When NO active grid (entry path):
1. Fetch available USDT balance from exchange
2. Call `AdaptiveGridPlanner.plan()` with current market data
3. Use returned AdaptiveGridPlan for grid placement
4. Persist adaptive values to state

### When ACTIVE grid:
1. Use persisted adaptive values (adaptive_lower_price, adaptive_upper_price, etc.)
2. Pass `adaptive_lower_price` to 15m boundary check (risk_engine.boundary_status)
3. Do NOT recalculate adaptive parameters mid-grid

### On restart:
1. Reconciliation loads persisted state
2. Active grid uses stored adaptive_lower_price, etc.
3. Only recalculates when no active grid exists

## Phase 5: Risk Engine (risk.py)

Boundary check uses `adaptive_lower_price` when available:
```python
# For active grid: use stored adaptive lower price
# For no grid (entry): planner computes candidate lower price first, validates freshness
```

## Phase 6: Dashboard (dashboard.py)

Expose for active symbols:
- `adaptive_lower_price`, `adaptive_upper_price`, `adaptive_total_grids`
- `adaptive_quote_budget`, `adaptive_grid_step`, `adaptive_reference_price`
- `adaptive_timeframe`

Filter to `configured_symbols` (already implemented) — hide historical symbols.

## Phase 7: Tests

Add regression tests covering:
- Config: no LOWER/UPPER/GRIDS/BUDGET required
- INDICATOR_TIMEFRAME remains required
- Automatic range/grid/budget calculation
- Candidate with net == 0.20% accepted, net < 0.20% rejected
- Tick-size/quantity-step/min-notional/max-notional quantization
- Quote reserve and multi-symbol allocation cap
- Insufficient balance blocks grid
- Missing/stale ATR/15m data blocks
- Active grid parameters locked (don't shift with ATR/price)
- New grid only after previous inactive
- Restart restores active adaptive params
- Historical symbols hidden
- No hardcoded TOTAL_GRIDS=5 fallback
- No hardcoded MIN_NOTIONAL_ESTIMATE

## Phase 8: Documentation

Update `.env.example` with adaptive parameters as specified.

## Validation
- pytest -q (298+ existing + new tests pass)
- python -m compileall -q .
- Repository-wide grep: no mandatory LOWER/UPPER/GRIDS/BUDGET, no MIN_NOTIONAL_ESTIMATE, no hardcoded 5-grid fallback

## Safety Confirmations
- No Binance orders submitted during development
- No VPS deployment/restart
- Live gates unchanged (DRY_RUN default, ALLOW_LIVE_EXECUTION default false)