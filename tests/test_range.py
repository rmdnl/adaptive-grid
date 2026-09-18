import pandas as pd
import numpy as np

from indicators import enrich
from range_engine import auto_range


def test_auto_range_returns_candidate():
    n = 160
    base = 100 + np.sin(np.linspace(0, 12, n)) * 3
    df = pd.DataFrame({
        "high": base + 1,
        "low": base - 1,
        "close": base,
        "volume": np.full(n, 1000.0),
    })
    df = enrich(df)
    result = auto_range(df, min_quality_score=0)
    assert result.lower > 0
    assert result.upper > result.lower
    assert 0 <= result.quality <= 100
