import numpy as np
import pandas as pd

def _true_range(df):
    prev = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs(),
    ], axis=1).max(axis=1)

def atr(df, length=14):
    return _true_range(df).rolling(length).mean()

def rsi(df, length=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0).rolling(length).mean()
    down = (-delta.clip(upper=0)).rolling(length).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def adx(df, length=14):
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _true_range(df)
    atr_v = tr.rolling(length).mean()
    plus_di = 100 * plus_dm.rolling(length).mean() / atr_v.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(length).mean() / atr_v.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(length).mean()

def bb_width(df, length=20, mult=2.0):
    mid = df["close"].rolling(length).mean()
    std = df["close"].rolling(length).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    return (upper - lower) / mid.replace(0, np.nan)

def volume_ratio(df, length=20):
    return df["volume"] / df["volume"].rolling(length).mean().replace(0, np.nan)

def enrich(df):
    out = df.copy()
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / out["close"]
    out["adx"] = adx(out)
    out["bb_width"] = bb_width(out)
    out["volume_ratio"] = volume_ratio(out)
    out["rsi"] = rsi(out)
    return out
