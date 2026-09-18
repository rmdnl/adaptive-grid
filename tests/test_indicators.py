import numpy as np
import pandas as pd
from indicators import adx, rsi, enrich

def make_df(n=120):
    x=np.linspace(100,105,n)
    return pd.DataFrame({"high":x+1,"low":x-1,"close":x,"volume":np.full(n,1000.0)})

def test_rsi_up_only_reaches_100():
    result=rsi(make_df())
    assert result.dropna().iloc[-1] == 100.0

def test_adx_has_valid_tail():
    assert adx(make_df()).dropna().shape[0] > 0

def test_enrich_contains_required_columns():
    result=enrich(make_df())
    for column in ("atr","atr_pct","adx","bb_width","volume_ratio","rsi"):
        assert column in result.columns
