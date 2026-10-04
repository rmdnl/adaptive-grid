from __future__ import annotations

import numpy as np
import pandas as pd

def _require_columns(df, columns):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

def _true_range(df):
    _require_columns(df, ("high", "low", "close"))
    prev = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)

def wilder_rma(series, length):
    if length <= 0:
        raise ValueError("length must be > 0")
    return series.ewm(alpha=1/length, adjust=False, min_periods=length).mean()

def atr(df, length=14):
    return wilder_rma(_true_range(df), length)

def rsi(df, length=14):
    _require_columns(df, ("close",))
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = wilder_rma(gain, length)
    avg_loss = wilder_rma(loss, length)

    out = pd.Series(np.nan, index=df.index, dtype=float)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    only_loss_zero = (avg_loss == 0) & (avg_gain > 0)
    normal = (~both_zero) & (~only_loss_zero)
    out.loc[only_loss_zero] = 100.0
    out.loc[normal] = 100 - (100 / (1 + avg_gain.loc[normal] / avg_loss.loc[normal]))
    out.loc[both_zero] = 50.0
    return out

def adx_components(df, length=14):
    _require_columns(df, ("high", "low", "close"))
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _true_range(df)
    atr_value = wilder_rma(tr, length)
    plus_di = 100 * wilder_rma(plus_dm, length) / atr_value.replace(0, np.nan)
    minus_di = 100 * wilder_rma(minus_dm, length) / atr_value.replace(0, np.nan)
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx_series = wilder_rma(dx, length)
    return adx_series, plus_di, minus_di

def adx(df, length=14):
    return adx_components(df, length)[0]

def bollinger_bands(df, length=20, mult=2.0):
    _require_columns(df, ("close",))
    if length <= 0:
        raise ValueError("length must be > 0")
    if mult <= 0:
        raise ValueError("mult must be > 0")
    mid = df["close"].rolling(length).mean()
    std = df["close"].rolling(length).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    width = upper - lower
    width_pct = width / mid.replace(0, np.nan)
    return mid, upper, lower, width, width_pct

def bb_width(df, length=20, mult=2.0):
    return bollinger_bands(df, length=length, mult=mult)[4]

def volume_baseline(df, length=20):
    _require_columns(df, ("volume",))
    if length <= 0:
        raise ValueError("length must be > 0")
    return df["volume"].shift(1).rolling(length).mean()

def volume_ratio(df, length=20):
    _require_columns(df, ("volume",))
    baseline = volume_baseline(df, length)
    return df["volume"] / baseline.replace(0, np.nan)

def volume_oscillator(df, short_period=5, long_period=10):
    """Volume Oscillator: (fast_volume_average / slow_volume_average) - 1

    Locked specification (section 10 of the strategy): SMA(volume, 5) and
    SMA(volume, 10); the oscillator is the RATIO minus one.  Positive values
    indicate expanding volume.  Zero does NOT satisfy an entry requirement
    of "> 0".  NaN rows (insufficient history) must be treated as
    INSUFFICIENT_DATA by the caller — never as 0.
    """
    _require_columns(df, ("volume",))
    if short_period >= long_period:
        raise ValueError("short_period must be < long_period")
    if short_period <= 0 or long_period <= 0:
        raise ValueError("periods must be > 0")

    fast_ma = df["volume"].rolling(short_period).mean()
    slow_ma = df["volume"].rolling(long_period).mean()

    # NaN (insufficient data) propagates; division by zero yields inf/NaN,
    # which the strategy layer treats as invalid, never as a signal.
    osc = (fast_ma / slow_ma) - 1
    return osc

def z_score(df, length=20):
    """Z-Score of close price: (close - mean) / std over lookback period
    
    Values > 2.5 or < -2.5 indicate extreme price deviation (statistical outlier)
    """
    _require_columns(df, ("close",))
    if length <= 0:
        raise ValueError("length must be > 0")
    
    rolling_mean = df["close"].rolling(length).mean()
    rolling_std = df["close"].rolling(length).std(ddof=0)
    
    # Avoid division by zero
    z = (df["close"] - rolling_mean) / rolling_std.replace(0, np.nan)
    return z

def enrich(df):
    if df.empty:
        raise ValueError("Empty market data")
    out = df.copy()
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / out["close"]
    out["adx"] = adx(out)
    out["bb_width"] = bb_width(out)
    out["volume_ratio"] = volume_ratio(out)
    out["rsi"] = rsi(out)
    out["volume_oscillator"] = volume_oscillator(out, short_period=5, long_period=10)
    out["z_score"] = z_score(out, length=20)
    return out

def latest_valid_row(df):
    required = ["close", "atr_pct", "adx", "bb_width", "volume_ratio", "rsi", "volume_oscillator", "z_score"]
    valid = df.dropna(subset=required)
    if valid.empty:
        raise ValueError("No fully valid indicator row available")
    return valid.iloc[-1]