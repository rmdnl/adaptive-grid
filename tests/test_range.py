import pandas as pd
from range_engine import auto_range

def test_auto_range():
    close = [100 + (i % 20) * 0.25 for i in range(100)]
    df = pd.DataFrame({
        "close": close,
        "adx": [15]*100,
        "atr_pct": [0.01]*100,
        "bb_width": [0.03]*100,
        "volume_ratio": [1.0]*100,
    })
    r = auto_range(df)
    assert r.lower > 0
    assert r.upper > r.lower
    assert 0 <= r.quality <= 100
