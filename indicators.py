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

def adx(df, length=14):
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
    return wilder_rma(dx, length)

def bb_width(df, length=20, mult=2.0):
    _require_columns(df, ("close",))
    mid = df["close"].rolling(length).mean()
    std = df["close"].rolling(length).std(ddof=0)
    return ((mid + mult*std) - (mid - mult*std)) / mid.replace(0, np.nan)

def volume_ratio(df, length=20):
    _require_columns(df, ("volume",))
    baseline = df["volume"].shift(1).rolling(length).mean()
    return df["volume"] / baseline.replace(0, np.nan)

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
    return out

def latest_valid_row(df):
    required = ["close", "atr_pct", "adx", "bb_width", "volume_ratio"]
    valid = df.dropna(subset=required)
    if valid.empty:
        raise ValueError("No fully valid indicator row available")
    return valid.iloc[-1]
