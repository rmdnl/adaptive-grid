"""Dashboard formatter edge-case tests: the EXACT shipped JavaScript
formatters (fmtNum / fmtPrice / fmtSigned / fmtPctFrac / fmtTs) are
extracted from render_page() and probed directly in the node runtime.
Skipped only when node.js is not installed.

Contract under test:
- missing data (null/undefined/NaN) renders the unavailable DASH;
- explicit zero renders a plain "0"/"0.00" (no sign, never a dash);
- values that ROUND to zero carry no sign ("-0.001" is not "-0.00");
- unset timestamps (<= 0) never render as a plausible wall-clock time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dashboard import render_page

NODE = shutil.which("node")
DASH = "\u2014"

_PROBE = r"""
const fs = require("fs");
const pagePath = process.argv[2];
const page = fs.readFileSync(pagePath, "utf8");
const js = page.split("<script>")[1].split("</script>")[0];

function grab(name) {
  const start = js.indexOf("function " + name + "(");
  if (start < 0) throw new Error("formatter not found: " + name);
  let i = js.indexOf("{", start), depth = 0, j = i;
  for (; j < js.length; j++) {
    if (js[j] === "{") depth++;
    else if (js[j] === "}") { depth--; if (depth === 0) break; }
  }
  return js.slice(start, j + 1);
}
const src = [
  'var DASH = "\\u2014";',
  grab("fmtNum"), grab("fmtPrice"), grab("fmtSigned"),
  grab("fmtPctFrac"), grab("fmtTs"),
  "return {fmtNum, fmtPrice, fmtSigned, fmtPctFrac, fmtTs};",
].join("\n");
const fns = new Function(src)();

const cases = {
  fmtNum_null: fns.fmtNum(null, 2),
  fmtNum_undef: fns.fmtNum(undefined, 2),
  fmtNum_nan: fns.fmtNum(NaN, 2),
  fmtNum_zero: fns.fmtNum(0, 0),
  fmtNum_qty: fns.fmtNum(0.00021, 8),
  fmtNum_big: fns.fmtNum(405275.83, 2),
  fmtPrice_null: fns.fmtPrice(null),
  fmtPrice_big: fns.fmtPrice(85582),
  fmtPrice_one: fns.fmtPrice(1),
  fmtPrice_tiny: fns.fmtPrice(0.00001234),
  fmtSigned_null: fns.fmtSigned(null),
  fmtSigned_zero: fns.fmtSigned(0),
  fmtSigned_neg_zero: fns.fmtSigned(-0.001),
  fmtSigned_pos: fns.fmtSigned(0.0735),
  fmtSigned_neg: fns.fmtSigned(-3.14159),
  fmtPctFrac_null: fns.fmtPctFrac(null),
  fmtPctFrac_gross: fns.fmtPctFrac(0.005),
  fmtPctFrac_net: fns.fmtPctFrac(0.002991),
  fmtPctFrac_neg: fns.fmtPctFrac(-0.02),
  fmtPctFrac_zero: fns.fmtPctFrac(0),
  fmtTs_null: fns.fmtTs(null),
  fmtTs_zero: fns.fmtTs(0),
  fmtTs_negative: fns.fmtTs(-5),
  fmtTs_real: fns.fmtTs(1791300075.0),
};
process.stdout.write(JSON.stringify(cases));
"""


def _probe(tmp_path: Path) -> dict:
    if NODE is None:
        pytest.skip("node.js runtime not available for JS formatter test")
    page_file = tmp_path / "page.html"
    probe_file = tmp_path / "probe.cjs"
    page_file.write_text(render_page(), encoding="utf-8")
    probe_file.write_text(_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(probe_file), str(page_file)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_missing_data_renders_dash(tmp_path):
    c = _probe(tmp_path)
    for key in ("fmtNum_null", "fmtNum_undef", "fmtNum_nan", "fmtPrice_null",
                "fmtSigned_null", "fmtPctFrac_null", "fmtTs_null"):
        assert c[key] == DASH, key


def test_explicit_zero_is_plain_zero_no_sign(tmp_path):
    c = _probe(tmp_path)
    assert c["fmtNum_zero"] == "0"
    assert c["fmtSigned_zero"] == "0.00"        # no "+0.00"
    assert c["fmtSigned_neg_zero"] == "0.00"    # -0.001 rounds to 0: no sign
    assert c["fmtPctFrac_zero"] == "0.00%"


def test_signed_values_use_plus_and_real_minus(tmp_path):
    c = _probe(tmp_path)
    assert c["fmtSigned_pos"] == "+0.07"
    assert c["fmtSigned_neg"] == "\u22123.14"   # U+2212 minus, not hyphen
    assert c["fmtPctFrac_gross"] == "+0.50%"
    assert c["fmtPctFrac_net"] == "+0.30%"
    assert c["fmtPctFrac_neg"] == "\u22122.00%"


def test_price_tiers_and_quantity_precision(tmp_path):
    c = _probe(tmp_path)
    assert c["fmtNum_qty"] == "0.00021000"
    assert c["fmtNum_big"] == "405,275.83"
    assert c["fmtPrice_big"] == "85,582.00"
    assert c["fmtPrice_one"] == "1.0000"
    assert c["fmtPrice_tiny"] == "0.00001234"


def test_unset_timestamps_never_render_as_time(tmp_path):
    c = _probe(tmp_path)
    assert c["fmtTs_zero"] == DASH
    assert c["fmtTs_negative"] == DASH
    assert c["fmtTs_real"] != DASH
    assert len(c["fmtTs_real"]) == 8            # HH:MM:SS
