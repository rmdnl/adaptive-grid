import pytest
from config_loader import ConfigError, validate_config

def base():
    return {
        "environment":{"mode":"testnet","dry_run":True,"allow_live_execution":False},
        "symbol":"BNBUSDT","timeframe":"15m",
        "grid":{"step_pct":0.006,"hard_min_net_pct":0.003,"preferred_net_max_pct":0.004,"min_cells":6,"max_levels":40},
        "range":{"mode":"auto","lower_price":0,"upper_price":0},
        "fees":{"maker_fee_fallback":0.001,"taker_fee_fallback":0.001,"slippage_roundtrip_pct":0.0005},
        "risk":{"max_equity_drawdown_pct":0.02,"range_break_buffer_pct":0.01},
        "execution":{"max_open_orders":40,"order_quote_size":25},
    }

def test_valid(): validate_config(base())

def test_unknown_mode_blocks():
    cfg=base(); cfg["environment"]["mode"]="tesnet"
    with pytest.raises(ConfigError): validate_config(cfg)

def test_dry_run_false_blocks():
    cfg=base(); cfg["environment"]["dry_run"]=False
    with pytest.raises(ConfigError): validate_config(cfg)
