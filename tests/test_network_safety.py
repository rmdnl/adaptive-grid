"""Network failure and retry safety (offline, urllib mocked).

Every failure mode surfaces as ExchangeError; only idempotent GETs are
retried; signed order endpoints are never blindly retried; DRY_RUN
refuses every signed request before any I/O can happen.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import exchange
from conftest import make_config
from exchange import BinanceSpot, ExchangeError


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _spot(dry_run=False) -> BinanceSpot:
    cfg = make_config(
        dry_run=dry_run,
        testnet_api_key="" if dry_run else "test-key",
        testnet_api_secret="" if dry_run else "test-secret",
    )
    return BinanceSpot(cfg)


def test_http_5xx_becomes_exchange_error(monkeypatch):
    spot = _spot()

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"boom")
        )

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ExchangeError, match="503"):
        spot.fetch_klines("BTC/USDT", "4h", 2)


def test_malformed_response_becomes_exchange_error(monkeypatch):
    spot = _spot()

    def fake_urlopen(req, timeout=10):
        return _FakeResponse(b"<html>gateway error page</html>")

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ExchangeError, match="malformed"):
        spot.fetch_klines("BTC/USDT", "4h", 2)


def test_get_retries_then_succeeds(monkeypatch):
    spot = _spot()
    calls = {"n": 0}
    payload = json.dumps([[0, "1", "2", "0.5", "1.5", "10", 999]]).encode()

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection reset")
        return _FakeResponse(payload)

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    klines = spot.fetch_klines("BTC/USDT", "4h", 2)
    assert calls["n"] == 3
    assert klines[0]["close"] == 1.5


def test_get_exhausts_retries_then_fails(monkeypatch):
    spot = _spot()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    with pytest.raises(ExchangeError, match="after retries"):
        spot.fetch_klines("BTC/USDT", "4h", 2)
    assert calls["n"] == 3  # initial attempt + 2 bounded retries


def test_signed_order_post_is_never_blindly_retried(monkeypatch):
    """A network failure on order submission must fail the request once —
    the executor reconciles by client id instead of resubmitting."""
    spot = _spot()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    with pytest.raises(ExchangeError):
        spot.create_market_order("BTC/USDT", "SELL", 1.0, "cid-x")
    assert calls["n"] == 1  # exactly one HTTP attempt, no blind retry


def test_dry_run_refuses_every_signed_request():
    """DRY_RUN=true must make every private/trading endpoint impossible to
    call — the refusal happens before any request is built."""
    spot = _spot(dry_run=True)
    signed_calls = (
        spot.get_account,
        lambda: spot.get_balance("BTC"),
        lambda: spot.create_limit_maker_order("BTC/USDT", "BUY", 1.0, 1.0, "c"),
        lambda: spot.create_market_order("BTC/USDT", "SELL", 1.0, "c"),
        lambda: spot.cancel_order("BTC/USDT", "c"),
        lambda: spot.get_order("BTC/USDT", "c"),
        lambda: spot.get_open_orders("BTC/USDT"),
        lambda: spot.get_my_trades("BTC/USDT"),
    )
    for fn in signed_calls:
        with pytest.raises(ExchangeError, match="DRY_RUN"):
            fn()


def test_live_env_without_live_gates_stays_on_testnet():
    """BINANCE_ENV=live alone must never select live endpoints or live
    credentials — all three gates are required."""
    cfg = make_config(
        binance_env="live",
        dry_run=True,
        allow_live_execution=True,
        live_api_key="k",
        live_api_secret="s",
    )
    assert cfg.allow_live is False
    spot = BinanceSpot(cfg)
    assert spot.base_url == exchange.TESTNET_BASE
    assert spot.environment == "TESTNET"


def test_gated_live_uses_live_endpoints():
    cfg = make_config(
        dry_run=False,
        allow_live_execution=True,
        binance_env="live",
        live_api_key="k",
        live_api_secret="s",
    )
    assert cfg.allow_live is True
    spot = BinanceSpot(cfg)
    assert spot.base_url == exchange.LIVE_BASE
    assert spot.environment == "LIVE"
    assert spot._api_key == "k" and spot._api_secret == "s"


def test_build_runtime_picks_dry_run_executor_by_default(tmp_path):
    from bot import build_runtime
    from exchange import DryRunExecutor
    from state import StateStore

    store = StateStore(str(tmp_path / "state.db"))
    bot, _market = build_runtime(make_config(), store)
    assert isinstance(bot.executor, DryRunExecutor)
