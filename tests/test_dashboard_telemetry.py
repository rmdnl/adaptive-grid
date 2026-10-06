"""Entry telemetry UI tests: /api/state payload, the real renderSymbols()
JavaScript path (executed via the system node runtime with a minimal DOM
stub — no browser, no added dependency), null-vs-zero distinction, and the
read-only guarantee.

The JS harness executes the EXACT <script> shipped in dashboard.render_page()
end-to-end: fetch("/api/state") -> JSON -> renderAll() -> renderSymbols() ->
DOM. It is skipped only when node.js is not installed (the strongest
deterministic test available without a browser).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from conftest import make_config
from dashboard import build_payload, render_page
from state import StateStore

NODE = shutil.which("node")
DASH = "\u2014"

TELEMETRY_ROWS = [
    ("EVALUATIONS", "entry_evaluations"),
    ("ADX BLOCKED", "blocked_adx"),
    ("STOCH CROSS BLOCKED", "blocked_stoch_cross"),
    ("STOCH K BLOCKED", "blocked_stoch_k"),
    ("GRID BLOCKED", "blocked_grid"),
    ("BUDGET BLOCKED", "blocked_budget"),
    ("RISK BLOCKED", "blocked_risk"),
    ("COOLDOWN BLOCKED", "blocked_cooldown"),
    ("EXIT PRIORITY", "blocked_exit_priority"),
    ("TOTAL ENTRIES", "entries_total"),
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_telemetry(store: StateStore, symbol: str, **values) -> None:
    full = dict(
        entry_evaluations=0, blocked_adx=0, blocked_stoch_cross=0, blocked_stoch_k=0,
        blocked_grid=0, blocked_budget=0, blocked_risk=0,
        blocked_cooldown=0, blocked_exit_priority=0, entries_total=0,
        last_entry_ts=None, last_entry_blocker=None, last_grid_reject_reason=None,
    )
    full.update(values)
    store.update_symbol(symbol, **full)


def _store_with_telemetry(tmp_path: Path) -> str:
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    symbols = ["BTC/USDT", "ETH/USDT"]
    store.ensure_symbols(symbols)
    store.set_meta("configured_symbols", json.dumps(symbols, separators=(",", ":")))
    _seed_telemetry(
        store, "BTC/USDT",
        entry_evaluations=128, blocked_adx=91, blocked_stoch_cross=14, blocked_stoch_k=8,
        blocked_grid=5, blocked_budget=0, blocked_risk=0,
        blocked_cooldown=3, blocked_exit_priority=4, entries_total=2,
        last_entry_ts=1791300075.0,
        last_entry_blocker="adx_not_low",
        last_grid_reject_reason="net_below_minimum",
        strategy_state="ACTIVE",
    )
    _seed_telemetry(
        store, "ETH/USDT",
        strategy_state="WAITING",   # all counters explicit 0, last_* None
    )
    return path


# ---------------------------------------------------------------------------
# 1. payload: /api/state includes entry_telemetry
# ---------------------------------------------------------------------------

def test_api_state_includes_entry_telemetry(tmp_path):
    path = _store_with_telemetry(tmp_path)
    payload = build_payload(path)
    btc = next(s for s in payload["symbols"] if s["symbol"] == "BTC/USDT")
    t = btc["entry_telemetry"]
    assert t["entry_evaluations"] == 128
    assert t["blocked_adx"] == 91
    assert t["blocked_stoch_cross"] == 14
    assert t["blocked_stoch_k"] == 8
    assert t["blocked_grid"] == 5
    assert t["blocked_budget"] == 0
    assert t["blocked_risk"] == 0
    assert t["blocked_cooldown"] == 3
    assert t["blocked_exit_priority"] == 4
    assert t["entries_total"] == 2
    assert t["last_entry_ts"] == 1791300075.0
    assert t["last_entry_blocker"] == "adx_not_low"
    assert t["last_grid_reject_reason"] == "net_below_minimum"


# ---------------------------------------------------------------------------
# JS path harness: the exact shipped <script> against a minimal DOM stub
# ---------------------------------------------------------------------------

_HARNESS = r"""
const fs = require("fs");
const pagePath = process.argv[2];
const payloadPath = process.argv[3];
const page = fs.readFileSync(pagePath, "utf8");
const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
const js = page.split("<script>")[1].split("</script>")[0];

class Ctx {}
["scale", "clearRect", "beginPath", "moveTo", "lineTo", "stroke", "fillText", "setLineDash"]
  .forEach(m => { Ctx.prototype[m] = function () {}; });

class El {
  constructor(tag) {
    this.tagName = tag;
    this.className = "";
    this.style = {};
    this._text = "";
    this._children = [];
    this.clientWidth = 600;
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this._children = []; }
  appendChild(ch) { this._children.push(ch); return ch; }
  setAttribute() {}
  addEventListener() {}
  getContext() { return new Ctx(); }
}

const registry = new Map();
globalThis.document = {
  getElementById(id) {
    if (!registry.has(id)) registry.set(id, new El("div"));
    return registry.get(id);
  },
  createElement(tag) { return new El(tag); },
};
globalThis.window = {
  addEventListener() {},
  devicePixelRatio: 1,
};
globalThis.setInterval = function () { return 0; };  // capture, never fire

globalThis.fetch = function (url) {
  if (url === "/api/state") {
    return Promise.resolve({ ok: true, json: async () => payload.state });
  }
  if (url === "/api/history") {
    return Promise.resolve({ ok: true, json: async () => payload.history });
  }
  return Promise.reject(new Error("unexpected url " + url));
};

function flatten(el, out) {
  out.push([el.className, el._text]);
  el._children.forEach(c => flatten(c, out));
}

(async () => {
  (0, eval)(js);  // run the shipped IIFE: tick() -> renderAll() -> DOM
  await new Promise(r => setTimeout(r, 40));
  const flat = [];
  flatten(globalThis.document.getElementById("symbols"), flat);
  process.stdout.write(JSON.stringify({ ok: true, flat }));
})().catch(e => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.stack || e) }));
  process.exit(0);
});
"""


def _run_js(payload_state: dict, tmp_path: Path) -> list:
    if NODE is None:
        pytest.skip("node.js runtime not available for JS-path test")
    page_file = tmp_path / "page.html"
    payload_file = tmp_path / "payload.json"
    harness_file = tmp_path / "harness.cjs"
    page_file.write_text(render_page(), encoding="utf-8")
    payload_file.write_text(json.dumps({"state": payload_state, "history": []}), encoding="utf-8")
    harness_file.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness_file), str(page_file), str(payload_file)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    result = json.loads(proc.stdout)
    assert result["ok"], f"page JS threw: {result.get('error')}"
    return result["flat"]


def _kv_pairs(flat):
    """The flat text stream contains .k/.v span pairs in order — pair them.
    Value spans may carry class modifiers ("v ok", "v cyan"), so match on
    the class prefix, not equality."""
    return [(flat[i][1], flat[i + 1][1]) for i in range(len(flat) - 1)
            if flat[i][0] == "k" and flat[i + 1][0].startswith("v")]


def _cards(flat):
    """Per-symbol card stream: cards begin at their .symname element; each
    holds its kv pairs in document order (pre-telemetry rows included)."""
    cards = []
    card = None
    i = 0
    while i < len(flat):
        cls, text = flat[i]
        if cls == "symname":
            card = {"symbol": text, "pairs": []}
            cards.append(card)
            i += 1
            continue
        if card is not None and i + 1 < len(flat) and cls == "k" and flat[i + 1][0].startswith("v"):
            card["pairs"].append((text, flat[i + 1][1]))
            i += 2
            continue
        i += 1
    return cards


def _card_rows(flat, occurrence: int = 0):
    """kv rows of the nth symbol card (cards appear in PAIR_LIST order)."""
    return dict(_cards(flat)[occurrence]["pairs"])


def _sections(flat):
    return [text for cls, text in flat if cls == "sec"]


def _full_payload(path: str, maxdd=None) -> dict:
    """Exactly what GET /api/state serves."""
    return json.loads(json.dumps(build_payload(path, maxdd)))


# ---------------------------------------------------------------------------
# 2-6. the shipped JS renders the telemetry section
# ---------------------------------------------------------------------------

def test_js_renders_entry_telemetry_section(tmp_path):
    path = _store_with_telemetry(tmp_path)
    flat = _run_js(_full_payload(path), tmp_path)
    assert "ENTRY TELEMETRY" in _sections(flat)
    rows = _card_rows(flat, occurrence=0)  # BTC card
    assert rows["EVALUATIONS"] == "128"
    assert rows["ADX BLOCKED"] == "91"
    assert rows["STOCH CROSS BLOCKED"] == "14"
    assert rows["STOCH K BLOCKED"] == "8"
    assert rows["GRID BLOCKED"] == "5"
    assert rows["BUDGET BLOCKED"] == "0"
    assert rows["RISK BLOCKED"] == "0"
    assert rows["COOLDOWN BLOCKED"] == "3"
    assert rows["EXIT PRIORITY"] == "4"
    assert rows["TOTAL ENTRIES"] == "2"


def test_js_last_entry_row_fields(tmp_path):
    path = _store_with_telemetry(tmp_path)
    flat = _run_js(_full_payload(path), tmp_path)
    rows = _card_rows(flat, occurrence=0)
    # last_entry_ts through the existing fmtTs formatter (en-GB time-of-day)
    expected = datetime.fromtimestamp(1791300075.0).strftime("%H:%M:%S")
    assert rows["LAST ENTRY"] == expected
    assert rows["LAST BLOCKER"] == "ADX ABOVE ENTRY LIMIT"          # mapped code
    assert rows["LAST GRID REJECT"] == "NET PROFIT BELOW MINIMUM"   # mapped phrase


def test_js_explicit_zero_displays_zero_not_dash(tmp_path):
    """0 means the backend explicitly reports zero — never the DASH."""
    path = _store_with_telemetry(tmp_path)
    flat = _run_js(_full_payload(path), tmp_path)
    rows = _card_rows(flat, occurrence=0)
    assert rows["BUDGET BLOCKED"] == "0"
    assert rows["RISK BLOCKED"] == "0"
    assert rows["TOTAL ENTRIES"] == "2"  # nonzero untouched


def test_js_missing_telemetry_renders_dash_without_crash(tmp_path):
    """A symbol payload without entry_telemetry (older database) must render
    the unavailable dash for every field and must not throw."""
    payload = _full_payload(_store_with_telemetry(tmp_path))
    eth = next(s for s in payload["symbols"] if s["symbol"] == "ETH/USDT")
    del eth["entry_telemetry"]
    flat = _run_js(payload, tmp_path)
    assert "ENTRY TELEMETRY" in _sections(flat)
    rows = _card_rows(flat, occurrence=1)  # ETH card
    for label, _field in TELEMETRY_ROWS:
        assert rows[label] == DASH, label
    assert rows["LAST ENTRY"] == DASH
    assert rows["LAST BLOCKER"] == DASH
    assert rows["LAST GRID REJECT"] == DASH


def test_js_partially_missing_fields_render_dash(tmp_path):
    """Missing individual fields render DASH while present fields render —
    and explicit zero still wins over the dash."""
    payload = _full_payload(_store_with_telemetry(tmp_path))
    eth = next(s for s in payload["symbols"] if s["symbol"] == "ETH/USDT")
    eth["entry_telemetry"] = {"blocked_adx": 3, "entries_total": 0}
    flat = _run_js(payload, tmp_path)
    rows = _card_rows(flat, occurrence=1)
    assert rows["ADX BLOCKED"] == "3"       # present -> rendered
    assert rows["TOTAL ENTRIES"] == "0"     # explicit zero -> "0", not dash
    assert rows["STOCH CROSS BLOCKED"] == DASH      # missing -> dash
    assert rows["EVALUATIONS"] == DASH
    assert rows["LAST BLOCKER"] == DASH


def test_js_blocker_fallback_for_unmapped_code(tmp_path):
    """Unknown technical codes display a safe readable form — no fabricated
    interpretation, no crash."""
    payload = _full_payload(_store_with_telemetry(tmp_path))
    btc = next(s for s in payload["symbols"] if s["symbol"] == "BTC/USDT")
    btc["entry_telemetry"]["last_entry_blocker"] = "some_future_code_here"
    btc["entry_telemetry"]["last_grid_reject_reason"] = "brand_new_reject_reason"
    flat = _run_js(payload, tmp_path)
    rows = _card_rows(flat, occurrence=0)
    assert rows["LAST BLOCKER"] == "SOME FUTURE CODE HERE"
    assert rows["LAST GRID REJECT"] == "BRAND NEW REJECT REASON"


def test_js_adaptive_planner_reject_reason_readable(tmp_path):
    payload = _full_payload(_store_with_telemetry(tmp_path))
    btc = next(s for s in payload["symbols"] if s["symbol"] == "BTC/USDT")
    btc["entry_telemetry"]["last_grid_reject_reason"] = (
        "adaptive_grid_failed: no valid grid found for BTC/USDT"
    )
    flat = _run_js(payload, tmp_path)
    rows = _card_rows(flat, occurrence=0)
    assert rows["LAST GRID REJECT"] == (
        "ADAPTIVE PLANNER: NO VALID GRID FOUND FOR BTC/USDT"
    )


def test_js_multi_symbol_telemetry_independent(tmp_path):
    """Both symbol cards render their OWN telemetry values, in PAIR_LIST
    order — no cross-symbol contamination."""
    path = _store_with_telemetry(tmp_path)
    flat = _run_js(_full_payload(path), tmp_path)
    pairs = _kv_pairs(flat)
    evals = [v for label, v in pairs if label == "EVALUATIONS"]
    adx = [v for label, v in pairs if label == "ADX BLOCKED"]
    entries = [v for label, v in pairs if label == "TOTAL ENTRIES"]
    exit_prio = [v for label, v in pairs if label == "EXIT PRIORITY"]
    blockers = [v for label, v in pairs if label == "LAST BLOCKER"]
    assert evals == ["128", "0"]
    assert adx == ["91", "0"]
    assert entries == ["2", "0"]
    assert exit_prio == ["4", "0"]
    # BTC has a recorded blocker; ETH's is genuinely unavailable -> dash
    assert blockers == ["ADX ABOVE ENTRY LIMIT", DASH]


# ---------------------------------------------------------------------------
# 7. existing rendering remains correct through the same JS path
# ---------------------------------------------------------------------------

def test_js_existing_price_and_state_rendering_unchanged(tmp_path):
    path = _store_with_telemetry(tmp_path)
    store = StateStore(path)
    store.update_symbol("BTC/USDT", last_price=50123.45, strategy_state="ACTIVE",
                        risk_status="ok", inventory_qty=0.002, avg_cost=50000.0)
    flat = _run_js(_full_payload(path), tmp_path)
    rows = _card_rows(flat, occurrence=0)
    assert rows["PRICE"] == "50,123.45"                  # existing fmtPrice path
    assert rows["STATE"] == "ACTIVE — GRID RUNNING"       # human state preserved
    assert rows["RISK"] == "OK — All risk gates clear"
    # unrealized PnL = 0.002 * (50123.45 - 50000.0) = 0.2469 -> "+0.25"
    assert rows["UNREALIZED PNL"] == "+0.25"


def test_js_adaptive_params_render_when_present(tmp_path):
    """Adaptive planner parameters surface in the GRID/POSITION section only
    for symbols that have them; symbols without never show fabricated rows."""
    path = _store_with_telemetry(tmp_path)
    store = StateStore(path)
    store.update_symbol(
        "BTC/USDT",
        adaptive_lower_price=47799.0, adaptive_upper_price=50100.0,
        adaptive_total_grids=5, adaptive_quote_budget=1200.5,
    )
    flat = _run_js(_full_payload(path), tmp_path)
    btc = _card_rows(flat, occurrence=0)
    eth = _card_rows(flat, occurrence=1)
    assert btc["LOWER BOUNDARY"] == "47,799.00"
    assert btc["RANGE HIGH"] == "50,100.00"
    assert btc["PLANNED GRIDS"] == "5"
    assert btc["QUOTE BUDGET"] == "1,200.50"
    # ETH has no adaptive record: rows absent, nothing fabricated
    for label in ("LOWER BOUNDARY", "RANGE HIGH", "PLANNED GRIDS", "QUOTE BUDGET"):
        assert label not in eth, label


def test_telemetry_section_uses_existing_css_classes(tmp_path):
    """The section reuses the existing visual language (sec/kv/hr): verified
    on the DOM the shipped JS actually produces."""
    path = _store_with_telemetry(tmp_path)
    flat = _run_js(_full_payload(path), tmp_path)
    classes = {cls for cls, _ in flat}
    assert {"sec", "k", "v", "hr"} <= classes
    assert any(c == "sec" and t == "ENTRY TELEMETRY" for c, t in flat)
    labels = [text for cls, text in flat if cls == "k"]
    for label, _field in TELEMETRY_ROWS:
        assert label in labels
    assert "LAST ENTRY" in labels
    assert "LAST BLOCKER" in labels
    assert "LAST GRID REJECT" in labels


# ---------------------------------------------------------------------------
# 8. read-only guarantee
# ---------------------------------------------------------------------------

def test_page_has_no_write_controls_or_unsafe_html():
    page = render_page()
    js = page.split("<script>")[1].split("</script>")[0]
    # no write controls anywhere in the page
    for marker in ("<button", "onclick", "onsubmit", "<form", "<input"):
        assert marker not in page.lower()
    # no trading-control vocabulary rendered as controls
    for word in ("BUY</", "SELL</", "CANCEL</", "RESET</", "KILL</", "RESUME</", "START</", "STOP</"):
        assert word not in page
    # dynamic values never via innerHTML; the shipped JS stays textContent-only
    assert "innerHTML" not in js
    assert "outerHTML" not in js
    assert "insertAdjacentHTML" not in js
    # fetch is only ever the two read endpoints, never with a method/body
    assert js.count("fetch(") == 2
    assert "/api/state" in js and "/api/history" in js
    assert "method" not in js and "body" not in js


def test_make_config_smoke():
    """Guard that the shared config still validates (no strategy drift from
    dashboard-only changes)."""
    cfg = make_config()
    assert cfg.entry_stoch_k_max == 0.3
    assert cfg.exit_stoch_k_max == 0.8
    assert cfg.dry_run is True
    assert cfg.allow_live_execution is False
