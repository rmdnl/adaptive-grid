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
        testnet_api_key="test-key",
        testnet_api_secret="test-secret",
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


def test_transient_http_5xx_get_is_retried_and_recovers(monkeypatch):
    """A transient 503 on a read-only GET is retried and recovers — one
    Binance blip must not flip a symbol into permanent ERROR state."""
    spot = _spot()
    calls = {"n": 0}
    payload = json.dumps([[0, "1", "2", "0.5", "1.5", "10", 999]]).encode()

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"boom")
            )
        return _FakeResponse(payload)

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    klines = spot.fetch_klines("BTC/USDT", "4h", 2)
    assert calls["n"] == 2
    assert klines[0]["close"] == 1.5


def test_persistent_http_5xx_get_still_fails_closed(monkeypatch):
    """Exhausted transient retries still surface ExchangeError — persistent
    exchange failure remains fail-closed, never a silent success."""
    spot = _spot()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"boom")
        )

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    with pytest.raises(ExchangeError, match="503"):
        spot.fetch_klines("BTC/USDT", "4h", 2)
    assert calls["n"] == 3  # bounded retries, then fail-closed


def test_order_post_http_5xx_is_never_retried(monkeypatch):
    """Even on a transient 503, an order POST is attempted exactly once —
    duplicate-order prevention outranks availability."""
    spot = _spot()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"boom")
        )

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(exchange.time, "sleep", lambda s: None)
    with pytest.raises(ExchangeError, match="503"):
        spot.create_market_order("BTC/USDT", "SELL", 1.0, "cid-y")
    assert calls["n"] == 1


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


def test_dry_run_refuses_every_trading_request(monkeypatch):
    """DRY_RUN=true must make every state-changing trading endpoint
    impossible to call — the refusal happens before any request is built.
    Read-only signed queries (account balance) stay available: paper mode
    needs them to derive session capital from the testnet wallet."""
    spot = _spot(dry_run=True)
    trading_calls = (
        lambda: spot.create_limit_maker_order("BTC/USDT", "BUY", 1.0, 1.0, "c"),
        lambda: spot.create_market_order("BTC/USDT", "SELL", 1.0, "c"),
        lambda: spot.cancel_order("BTC/USDT", "c"),
    )
    for fn in trading_calls:
        with pytest.raises(ExchangeError, match="DRY_RUN"):
            fn()
    # read-only signed access remains (paper capital initialization)
    def fake_urlopen(req, timeout=10):
        return _FakeResponse(json.dumps({"canTrade": True, "balances": []}).encode())

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    assert spot.get_balance("USDT") == 0.0


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
        execution_mode="live",
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


def test_validate_trading_access_checks_all_gates(monkeypatch):
    """The startup gate verifies clock skew, canTrade, USDT balance and
    per-symbol tradability — failing closed on the first problem."""
    spot = _spot()

    class FakeTimeResponse(_FakeResponse):
        pass

    good_account = json.dumps({
        "canTrade": True,
        "balances": [{"asset": "USDT", "free": "10000", "locked": "0"}],
    }).encode()
    good_exchangeinfo = json.dumps({
        "symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "10"},
            ],
        }],
    }).encode()

    def fake_urlopen(req, timeout=10):
        if "api/v3/time" in req.full_url:
            server_now = int(__import__("time").time() * 1000)
            return _FakeResponse(json.dumps({"serverTime": server_now}).encode())
        if "api/v3/account" in req.full_url:
            return _FakeResponse(good_account)
        if "api/v3/exchangeInfo" in req.full_url:
            return _FakeResponse(good_exchangeinfo)
        raise AssertionError(f"unexpected endpoint {req.full_url}")

    monkeypatch.setattr(exchange.urllib.request, "urlopen", fake_urlopen)
    access = spot.validate_trading_access(["BTC/USDT"])
    assert access["usdt"] == 10000.0
    assert access["clock_skew_ms"] < 30_000

    # non-trading symbol -> refused
    halted = good_exchangeinfo.replace(b'"TRADING"', b'"BREAK"')
    def halted_urlopen(req, timeout=10):
        if "api/v3/time" in req.full_url:
            return _FakeResponse(json.dumps({"serverTime": int(__import__("time").time() * 1000)}).encode())
        if "api/v3/account" in req.full_url:
            return _FakeResponse(good_account)
        return _FakeResponse(halted)
    monkeypatch.setattr(exchange.urllib.request, "urlopen", halted_urlopen)
    with pytest.raises(ExchangeError, match="not tradable"):
        spot.validate_trading_access(["BTC/USDT"])

    # cannot-trade account -> refused
    no_trade = good_account.replace(b"true", b"false")
    def no_trade_urlopen(req, timeout=10):
        if "api/v3/time" in req.full_url:
            return _FakeResponse(json.dumps({"serverTime": int(__import__("time").time() * 1000)}).encode())
        return _FakeResponse(no_trade)
    monkeypatch.setattr(exchange.urllib.request, "urlopen", no_trade_urlopen)
    with pytest.raises(ExchangeError, match="cannot trade"):
        spot.validate_trading_access(["BTC/USDT"])

    # excessive clock skew -> refused
    def skewed_urlopen(req, timeout=10):
        if "api/v3/time" in req.full_url:
            old = int(__import__("time").time() * 1000) - 120_000
            return _FakeResponse(json.dumps({"serverTime": old}).encode())
        raise AssertionError("should not be reached")
    monkeypatch.setattr(exchange.urllib.request, "urlopen", skewed_urlopen)
    with pytest.raises(ExchangeError, match="clock"):
        spot.validate_trading_access(["BTC/USDT"])
