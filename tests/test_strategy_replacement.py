"""Old-strategy removal regression tests (hard requirement).

Prove the previous strategy can no longer generate orders:

- the legacy single-symbol entrypoint (main.py) and its runtime wiring are
  gone from the repository;
- the old eligibility gate (grid_eligibility) and the old strategy-specific
  market_filter gate are removed from the strategy path and configuration;
- the NEW strategy is the only authoritative signal decision path, with the
  locked thresholds and no Volume Oscillator gating.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import config_loader
import multi_symbol_main as msm
import strategy

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_legacy_single_symbol_entrypoint_is_gone():
    assert not (REPO_ROOT / "main.py").exists(), (
        "main.py (old single-symbol strategy path) must be removed")
    assert not (REPO_ROOT / "deploy" / "adaptive-grid.service").exists(), (
        "the legacy systemd unit must be removed")


def test_old_eligibility_gate_is_gone():
    assert not (REPO_ROOT / "grid_eligibility.py").exists()
    source = inspect.getsource(msm)
    assert "grid_eligibility" not in source
    assert "evaluate_grid_eligibility" not in source


def test_market_filter_gate_removed_from_strategy_path():
    source = inspect.getsource(msm)
    assert "market_gate" not in source
    assert 'cfg["market_filter"]' not in source
    # ...and from the configuration itself.
    cfg = config_loader.load_config()
    assert "market_filter" not in cfg


def test_old_thresholds_are_gone_from_configuration():
    """ADX<20 / RSI<35 / VO>0 entry logic is replaced by the locked v5
    thresholds (ADX<25, RSI<40, %B<=0; VO diagnostics only)."""
    cfg = config_loader.load_config()
    entry = cfg["strategy"]["entry"]
    assert str(entry["adx_max"]) in ("25", "25.0")
    assert str(entry["rsi_max"]) in ("40", "40.0")
    assert "require_bollinger_touch" not in entry


def test_volume_oscillator_cannot_gate_entry():
    """Static check: the entry evaluator never reads a VO threshold for its
    decision — the only volume_oscillator use in strategy.py is reporting."""
    source = inspect.getsource(strategy)
    tree = ast.parse(source)
    entry_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "evaluate_entry_signal")
    entry_source = ast.get_source_segment(source, entry_fn)
    assert "vol_osc_ok" not in entry_source, (
        "the entry decision must not gate on the Volume Oscillator")


def test_runtime_module_is_orchestration_only():
    """runtime.py (kept as shared GridRuntime infrastructure) must not
    import the removed legacy strategy or any trading module."""
    runtime_source = (REPO_ROOT / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(runtime_source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for banned in ("main", "grid_eligibility", "order_engine",
                   "testnet_orders", "cancel_controller"):
        assert banned not in imported


def test_new_strategy_is_the_only_entrypoint():
    """The multi-symbol entrypoint owns the authoritative cycle."""
    assert (REPO_ROOT / "multi_symbol_main.py").exists()
    source = inspect.getsource(msm)
    assert "evaluate_strategy" in source
    assert "StrategyStateTracker" in source
    # Strategy signal evaluation flows exclusively through strategy.py.
    tree = ast.parse(source)
    calls = {node.func.attr if isinstance(node.func, ast.Attribute)
             else node.func.id
             for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "evaluate_strategy" in calls
