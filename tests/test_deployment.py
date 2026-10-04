"""Deployment correctness: systemd templates match the running
architecture, and no stale architecture references return to the repo."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TRADING_UNIT = REPO / "deploy" / "adaptive-grid.service"
DASHBOARD_UNIT = REPO / "deploy" / "adaptive-grid-dashboard.service"


def test_trading_unit_matches_running_architecture():
    unit = TRADING_UNIT.read_text(encoding="utf-8")
    assert "User=adaptive-grid" in unit
    assert "WorkingDirectory=/opt/adaptive-grid" in unit
    assert (
        "ExecStart=/opt/adaptive-grid/.venv/bin/python bot.py "
        "--env /opt/adaptive-grid/.env --db /opt/adaptive-grid/state.db" in unit
    )
    assert "Restart=on-failure" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "EnvironmentFile=-/opt/adaptive-grid/.env" in unit  # bot reads .env


def test_dashboard_unit_uses_authoritative_database_and_flags():
    unit = DASHBOARD_UNIT.read_text(encoding="utf-8")
    assert (
        "ExecStart=/opt/adaptive-grid/.venv/bin/python dashboard.py "
        "--host 0.0.0.0 --port 8080 --db /opt/adaptive-grid/state.db" in unit
    )


def test_dashboard_unit_does_not_receive_env_file():
    """The dashboard must never be handed .env — it reads no configuration
    and no credentials; everything arrives via CLI flags."""
    unit = DASHBOARD_UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile" not in unit


def test_units_use_the_same_authoritative_database():
    """Exactly one state database: both services must reference
    /opt/adaptive-grid/state.db — never a second/legacy database."""
    trading = TRADING_UNIT.read_text(encoding="utf-8")
    dashboard = DASHBOARD_UNIT.read_text(encoding="utf-8")
    assert "--db /opt/adaptive-grid/state.db" in trading
    assert "--db /opt/adaptive-grid/state.db" in dashboard
    assert "grid_bot.sqlite3" not in trading + dashboard
    assert "data/" not in dashboard.replace("data/", "data/", 1) or "--db /opt/adaptive-grid/data" not in dashboard


def test_units_are_hardened():
    for unit in (TRADING_UNIT, DASHBOARD_UNIT):
        text = unit.read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in text
        assert "ProtectSystem=full" in text
        assert "ProtectHome=true" in text
        assert "PrivateTmp=true" in text


def test_no_stale_references_in_deployed_files():
    for path in (TRADING_UNIT, DASHBOARD_UNIT):
        text = path.read_text(encoding="utf-8")
        for stale in ("runtime.py", "config.yaml", "paper_orch_cycles", "grid_bot.sqlite3"):
            assert stale not in text, (path.name, stale)


def test_no_stale_references_in_runtime_sources():
    """Lock the clean architecture: no legacy module/config/database
    references may re-enter runtime code or deployment docs."""
    forbidden = ("runtime.py", "config.yaml", "paper_orch_cycles", "grid_bot.sqlite3")
    scan_targets = [REPO / "README.md"] + [
        p for p in (REPO).glob("*.py")
    ] + list((REPO / "deploy").glob("*"))
    for target in scan_targets:
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        for stale in forbidden:
            assert stale not in text, (target.name, stale)


def test_dashboard_source_never_touches_configuration_or_secrets():
    """The dashboard imports no configuration and no credential material:
    it may only read the state database it is pointed at."""
    source = (REPO / "dashboard.py").read_text(encoding="utf-8")
    for forbidden in ("dotenv", "load_dotenv", "os.environ", "API_KEY", "API_SECRET",
                      "BINANCE_TESTNET", "BINANCE_LIVE", "api_secret"):
        assert forbidden not in source, forbidden


def test_env_example_carries_execution_mode_defaults():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "EXECUTION_MODE=paper" in env
    assert "BINANCE_ENV=testnet" in env
    assert "DRY_RUN=true" in env
    assert "ALLOW_LIVE_EXECUTION=false" in env
    # production keys use the new names; testnet keys separate
    assert "BINANCE_API_KEY=" in env and "BINANCE_API_SECRET=" in env
    assert "BINANCE_TESTNET_API_KEY=" in env
    assert "BINANCE_LIVE_API_KEY" not in env


def test_no_hardcoded_default_capital():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "START_EQUITY=1000" not in env
