"""Deterministic Phase 4 Market Features Calculation.

Calculates pure, deterministic market features (ATR, ADX, Bollinger Bands,
volume baseline, price range stability, liquidity spread, Volume Oscillator,
Z-Score) using closed candles and read-only market quotes.

Design Principles:
- Fails closed on malformed, incomplete, non-monotonic or insufficient candle data.
- Never uses the currently forming candle.
- Reuses existing project utilities where available.
- Pure functions: no external state mutation, no network calls, no randomness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd

from indicators import adx_components, atr, bollinger_bands, volume_baseline, rsi, volume_oscillator, z_score
from market_data import MarketQuote


class MarketIntelligenceError(Exception):
    """Base exception for Phase 4 market intelligence errors."""


class CandleValidationError(MarketIntelligenceError):
    """Raised when candle data is malformed, non-monotonic, or invalid."""


class InsufficientDataError(CandleValidationError):
    """Raised when the candle history has fewer rows than required."""


def _to_decimal(value: Any, name: str = "value") -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CandleValidationError(f"Invalid decimal for {name}: {value!r}") from exc
    if not dec.is_finite():
        raise CandleValidationError(f"Non-finite decimal for {name}: {value!r}")
    return dec


def validate_candles(
    df: pd.DataFrame,
    min_candles: int = 60,
    max_candle_age_seconds: int | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Validate closed candle data strictly and fail closed on any defect.

    Checks:
    - Non-empty DataFrame with required OHLCV columns.
    - At least ``min_candles`` rows.
    - Positivity: open, high, low, close > 0; volume >= 0.
    - OHLC validity: high >= low, high >= open, high >= close,
      low <= open, low <= close.
    - Timestamps: open_time < close_time for each row.
    - Strictly monotonic increasing timestamps (no duplicates, no backward jumps).
    - Closed candle enforcement: no candle whose close_time is in the future
      relative to ``now`` (forming candle excluded).
    - Freshness: last candle close_time within ``max_candle_age_seconds`` if supplied.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        raise CandleValidationError("Candle data is missing or empty")

    required_cols = ("open_time", "close_time", "open", "high", "low", "close", "volume")
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise CandleValidationError(f"Candle data missing required columns: {missing}")

    if len(df) < min_candles:
        raise InsufficientDataError(
            f"Insufficient candles: got {len(df)}, required at least {min_candles}"
        )

    # Check for NaN / null in any required column
    if df[list(required_cols)].isna().any().any():
        raise CandleValidationError("Candle data contains NaN or null values")

    # Numeric conversion & positivity checks
    for col in ("open", "high", "low", "close"):
        s = pd.to_numeric(df[col], errors="coerce")
        if s.isna().any():
            raise CandleValidationError(f"Candle column {col} contains non-numeric values")
        if (s <= 0).any():
            raise CandleValidationError(f"Candle column {col} contains non-positive values")

    vol = pd.to_numeric(df["volume"], errors="coerce")
    if vol.isna().any():
        raise CandleValidationError("Candle column volume contains non-numeric values")
    if (vol < 0).any():
        raise CandleValidationError("Candle column volume contains negative values")

    # OHLC structural relationship checks
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    opens = df["open"].astype(float)
    closes = df["close"].astype(float)

    if (highs < lows).any():
        raise CandleValidationError("Candle high < low detected")
    if (highs < opens).any():
        raise CandleValidationError("Candle high < open detected")
    if (highs < closes).any():
        raise CandleValidationError("Candle high < close detected")
    if (lows > opens).any():
        raise CandleValidationError("Candle low > open detected")
    if (lows > closes).any():
        raise CandleValidationError("Candle low > close detected")

    # Timestamp checks
    open_times = pd.to_datetime(df["open_time"], utc=True)
    close_times = pd.to_datetime(df["close_time"], utc=True)

    if (open_times >= close_times).any():
        raise CandleValidationError("Candle open_time >= close_time detected")

    # Strictly monotonic (no duplicates, no backward steps)
    if not open_times.is_monotonic_increasing or open_times.duplicated().any():
        raise CandleValidationError("Candle open_time is not strictly monotonic increasing")
    if not close_times.is_monotonic_increasing or close_times.duplicated().any():
        raise CandleValidationError("Candle close_time is not strictly monotonic increasing")

    # Closed candle enforcement: reject if any candle close_time > now
    if now is not None:
        if now.tzinfo is None:
            raise CandleValidationError("Reference 'now' must be timezone-aware (UTC)")
        now_ts = pd.Timestamp(now)
        if (close_times > now_ts).any():
            raise CandleValidationError(
                "Incomplete or currently forming candle detected: candle close_time is in the future"
            )

        if max_candle_age_seconds is not None:
            if max_candle_age_seconds <= 0:
                raise CandleValidationError("max_candle_age_seconds must be positive")
            last_close = close_times.iloc[-1]
            age_sec = (now_ts - last_close).total_seconds()
            if age_sec < 0:
                raise CandleValidationError("Last candle close_time is in the future")
            if age_sec > max_candle_age_seconds:
                raise CandleValidationError(
                    f"Candle data is stale: age {age_sec:.1f}s exceeds max {max_candle_age_seconds}s"
                )

    return df


@dataclass(frozen=True)
class MarketFeatures:
    """Deterministic market features calculated from closed candles."""

    symbol: str
    close_price: Decimal
    atr: Decimal
    atr_pct: Decimal
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal
    bb_middle: Decimal
    bb_upper: Decimal
    bb_lower: Decimal
    bb_width: Decimal
    bb_width_pct: Decimal
    current_volume: Decimal
    baseline_volume: Decimal
    volume_spike_ratio: Decimal
    directional_efficiency: Decimal
    atr_expansion_ratio: Decimal
    range_containment_pct: Decimal
    penetration_count: int
    spread: Decimal | None
    spread_pct: Decimal | None
    rsi: Decimal
    volume_oscillator: Decimal
    z_score: Decimal


def calculate_market_features(
    df: pd.DataFrame,
    quote: MarketQuote | None,
    lower_price: Decimal,
    upper_price: Decimal,
    symbol: str,
    config: dict[str, Any],
    now: datetime | None = None,
) -> MarketFeatures:
    """Calculate all Phase 4 market features deterministically.

    Formulas:
    1. ATR: Wilder RMA of True Range over atr_period.
       atr_pct = atr / close
    2. ADX: Wilder RMA of DX over adx_period, with +DI and -DI.
    3. Bollinger Bands: length = bb_length, multiplier = bb_std_mult.
       width = upper - lower = 2 * mult * std
       width_pct = width / middle
    4. Volume:
       baseline_volume = mean(volume[-(period+1):-1])
       volume_spike_ratio = current_volume / baseline_volume
    5. Range Stability:
       - directional_efficiency = |close[-1] - close[-N]| / sum(|close[i] - close[i-1]|)
         (Kaufman efficiency ratio over stability window N)
       - atr_expansion_ratio = current_atr / mean(atr[-N:])
       - range_containment_pct = count(low >= lower and high <= upper) / N
       - penetration_count = N - contained_count
    6. Liquidity / Spread:
       spread = ask_price - bid_price
       spread_pct = spread / mid_price
    7. RSI: Wilder RMA over 14 periods
    8. Volume Oscillator: (Short MA - Long MA) / Long MA * 100 (periods 5, 10)
    9. Z-Score: (close - mean) / std over 20 periods
    """
    mi = config.get("market_intelligence", config)
    min_candles = int(mi.get("min_candles", 60))
    max_candle_age = mi.get("max_candle_age_seconds")
    atr_period = int(mi.get("atr_period", 14))
    adx_period = int(mi.get("adx_period", 14))
    bb_length = int(mi.get("bb_length", 20))
    bb_std_mult = float(mi.get("bb_std_mult", 2.0))
    vol_period = int(mi.get("volume_baseline_period", 20))
    stability_period = int(mi.get("range_stability_period", 20))
    vol_osc_short = int(mi.get("volume_oscillator_short_period", 5))
    vol_osc_long = int(mi.get("volume_oscillator_long_period", 10))
    zscore_period = int(mi.get("zscore_period", 20))

    validated_df = validate_candles(
        df,
        min_candles=min_candles,
        max_candle_age_seconds=max_candle_age,
        now=now,
    )

    if lower_price <= 0:
        raise CandleValidationError("lower_price must be > 0 for range stability calculation")
    if upper_price <= lower_price:
        raise CandleValidationError(
            f"upper_price ({upper_price}) must be > lower_price ({lower_price})"
        )

    # 1. ATR & ATR percentage
    atr_series = atr(validated_df, length=atr_period)
    last_atr_val = atr_series.dropna()
    if last_atr_val.empty:
        raise InsufficientDataError("Unable to calculate ATR from supplied candle history")
    last_close = _to_decimal(validated_df["close"].iloc[-1], "close")
    atr_dec = _to_decimal(round(last_atr_val.iloc[-1], 8), "atr")
    atr_pct_dec = atr_dec / last_close

    # 2. ADX, +DI, -DI
    adx_series, plus_di_series, minus_di_series = adx_components(validated_df, length=adx_period)
    valid_adx = adx_series.dropna()
    if valid_adx.empty:
        raise InsufficientDataError("Unable to calculate ADX from supplied candle history")
    adx_dec = _to_decimal(round(valid_adx.iloc[-1], 4), "adx")
    plus_di_dec = _to_decimal(round(plus_di_series.dropna().iloc[-1], 4), "plus_di")
    minus_di_dec = _to_decimal(round(minus_di_series.dropna().iloc[-1], 4), "minus_di")

    # 3. Bollinger Bands
    bb_mid, bb_upper, bb_lower, bb_width, _ = bollinger_bands(
        validated_df, length=bb_length, mult=bb_std_mult
    )
    if bb_mid.dropna().empty:
        raise InsufficientDataError("Unable to calculate Bollinger Bands from candle history")
    bb_mid_dec = _to_decimal(round(bb_mid.dropna().iloc[-1], 8), "bb_middle")
    bb_upper_dec = _to_decimal(round(bb_upper.dropna().iloc[-1], 8), "bb_upper")
    bb_lower_dec = _to_decimal(round(bb_lower.dropna().iloc[-1], 8), "bb_lower")
    bb_width_dec = bb_upper_dec - bb_lower_dec
    bb_width_pct_dec = bb_width_dec / bb_mid_dec

    # 4. Volume baseline and spike ratio
    vol_base_series = volume_baseline(validated_df, length=vol_period)
    valid_vol_base = vol_base_series.dropna()
    if valid_vol_base.empty:
        raise InsufficientDataError("Unable to calculate volume baseline from candle history")
    current_vol_dec = _to_decimal(round(validated_df["volume"].iloc[-1], 8), "current_volume")
    base_vol_dec = _to_decimal(round(valid_vol_base.iloc[-1], 8), "baseline_volume")
    if base_vol_dec <= 0:
        # Fall back to 1.0 ratio if baseline volume is zero
        vol_spike_ratio_dec = Decimal("1.0")
    else:
        vol_spike_ratio_dec = current_vol_dec / base_vol_dec

    # 5. RSI (period 14)
    rsi_series = rsi(validated_df, length=adx_period)  # reuse adx_period (14) for RSI
    valid_rsi = rsi_series.dropna()
    if valid_rsi.empty:
        raise InsufficientDataError("Unable to calculate RSI from supplied candle history")
    rsi_dec = _to_decimal(round(valid_rsi.iloc[-1], 2), "rsi")

    # 6. Volume Oscillator (short=5, long=10)
    vol_osc_series = volume_oscillator(validated_df, short_period=vol_osc_short, long_period=vol_osc_long)
    valid_vol_osc = vol_osc_series.dropna()
    if valid_vol_osc.empty:
        raise InsufficientDataError("Unable to calculate Volume Oscillator from supplied candle history")
    vol_osc_dec = _to_decimal(round(valid_vol_osc.iloc[-1], 4), "volume_oscillator")

    # 7. Z-Score (period 20)
    zscore_series = z_score(validated_df, length=zscore_period)
    valid_zscore = zscore_series.dropna()
    if valid_zscore.empty:
        raise InsufficientDataError("Unable to calculate Z-Score from supplied candle history")
    zscore_dec = _to_decimal(round(valid_zscore.iloc[-1], 4), "z_score")

    # 8. Range Stability metrics
    if len(validated_df) < stability_period:
        raise InsufficientDataError(
            f"Not enough candles for range stability: {len(validated_df)} < {stability_period}"
        )
    sub = validated_df.iloc[-stability_period:]
    sub_closes = sub["close"].astype(float).values
    net_path = abs(sub_closes[-1] - sub_closes[0])
    gross_path = float(np.sum(np.abs(np.diff(sub_closes))))
    if gross_path > 1e-12:
        directional_eff_val = round(net_path / gross_path, 4)
    else:
        directional_eff_val = 0.0
    directional_eff_dec = _to_decimal(directional_eff_val, "directional_efficiency")

    # ATR expansion ratio = current ATR / mean ATR over stability window
    atr_window = atr_series.iloc[-stability_period:].dropna()
    if not atr_window.empty and atr_window.mean() > 0:
        atr_expansion_val = round(float(last_atr_val.iloc[-1]) / float(atr_window.mean()), 4)
        atr_expansion_dec = _to_decimal(atr_expansion_val, "atr_expansion_ratio")
    else:
        atr_expansion_dec = Decimal("1.0")

    # Range containment & penetration count
    sub_highs = [Decimal(str(h)) for h in sub["high"]]
    sub_lows = [Decimal(str(l)) for l in sub["low"]]
    contained_count = 0
    for h, l in zip(sub_highs, sub_lows):
        if l >= lower_price and h <= upper_price:
            contained_count += 1
    containment_pct_dec = Decimal(contained_count) / Decimal(stability_period)
    penetration_count = stability_period - contained_count

    # 9. Liquidity / Quote
    spread_dec: Decimal | None = None
    spread_pct_dec: Decimal | None = None
    if quote is not None:
        if quote.bid_price <= 0 or quote.ask_price <= 0:
            raise CandleValidationError(f"Non-positive quote prices for {symbol}")
        if quote.ask_price < quote.bid_price:
            raise CandleValidationError(f"Crossed book quote for {symbol}: ask < bid")
        spread_dec = quote.spread
        spread_pct_dec = quote.spread_pct

    return MarketFeatures(
        symbol=symbol,
        close_price=last_close,
        atr=atr_dec,
        atr_pct=atr_pct_dec,
        adx=adx_dec,
        plus_di=plus_di_dec,
        minus_di=minus_di_dec,
        bb_middle=bb_mid_dec,
        bb_upper=bb_upper_dec,
        bb_lower=bb_lower_dec,
        bb_width=bb_width_dec,
        bb_width_pct=bb_width_pct_dec,
        current_volume=current_vol_dec,
        baseline_volume=base_vol_dec,
        volume_spike_ratio=vol_spike_ratio_dec,
        directional_efficiency=directional_eff_dec,
        atr_expansion_ratio=atr_expansion_dec,
        range_containment_pct=containment_pct_dec,
        penetration_count=penetration_count,
        spread=spread_dec,
        spread_pct=spread_pct_dec,
        rsi=rsi_dec,
        volume_oscillator=vol_osc_dec,
        z_score=zscore_dec,
    )