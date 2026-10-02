# AUTONOMOUS RUN REPORT — adaptive-grid

Run date: 2026-10-02 (UTC+7). Branch: `hermes/autonomous`.

## Test result

- Baseline (half-applied PATCH 1 in working tree): **19 failed, 1012 passed** (214s).
- After this run: **1031 passed, 0 failed** (237s), full `pytest -q`.
- Targeted (kill-switch + main integration suites): 34/34 passed.

## Summary of changes

Completed the half-applied "PATCH 1 (F-H1): persist the equity-drawdown kill-switch
reference" work that was left in the working tree (in-progress edits to `main.py`,
`risk_engine.py`, `tests/test_paper_validation.py`, new `tests/test_kill_switch_persistence.py`).

Root cause of the baseline failures: `main()` still referenced the deleted global
`_SESSION_REFERENCE_EQUITY` (NameError in production; `monkeypatch.setattr`
AttributeError in 19 integration tests), and the new persistence helpers
(`load_peak_equity`, `record_peak_equity`, `equity_reference_gate`) were never
wired into the risk path.

Changes (commit `288349d`):

- `main.py`
  - Removed the dead `_SESSION_REFERENCE_EQUITY` global usage.
  - `raw_reference`/`reference_equity` are now read from the persisted
    `paper_reference_equity` bot_state key on every run (unconditionally), and
    passed to `build_account_risk_state` (None when absent → bootstrap).
  - Peak equity recorded (high-water mark only: raises, never lowers) after
    every successful equity observation, and NOT recorded when a present
    reference is corrupt — silent "repair" would defeat the kill switch.
  - `equity_reference_gate(raw_reference, reference_equity)` added to the
    combined risk decisions: absent reference passes; corrupt reference blocks
    with `EQUITY_REFERENCE_INVALID` (fail-closed).
- `risk_engine.py` — `equity_reference_gate` (present in working tree; kept,
  now actually used by `main()`).
- `tests/test_kill_switch_persistence.py` (new, 9 tests) — all pass:
  persistence across restart, no down-reset of the peak, healthy-growth update,
  corrupt-state fail-closed load, absent-key load, drawdown block through the
  real `main.main()` production path, corrupt-reference block, open-book
  preservation under a vetoed submission, no-kill-switch on equity growth.
  - One exchange-stub fix: this file's tests use the shared `_symbol_info()`
    (±5% PERCENT_PRICE band), which made grid cells near the manual range edge
    get rejected by quantization before the kill switch could be the sole block
    reason. Added `_wide_percent_price_symbol_info()` (±10% band, matching the
    actual BNBUSDT testnet band) so the kill switch is isolated under test.
- `tests/test_paper_validation.py` — machine-independent repo-root paths for
    static source-scan tests (was hardcoded `c:/Projects/adaptive-grid/...`).
- `tests/test_main_order_integration.py`, `tests/test_main_market_intelligence.py`,
  `tests/test_main_adaptive_planner.py` — removed stale `monkeypatch.setattr(main,
  "_SESSION_REFERENCE_EQUITY", None)` lines referencing the deleted global
  (not a weakening: the reference now lives in the fresh per-test SQLite DB).
- `README.md` — "Account-risk state" section now documents the persisted
  high-water-mark reference and its fail-closed behavior (previously documented
  the old session-local, non-persisted semantics).
- Committed the repo's contract files `AGENTS.md` and `AUTONOMOUS_TASK.md`
  (untracked on-disk, part of the project's contract).

## Commit hashes

- `288349d` feat: persist equity-drawdown kill-switch reference across restarts (PATCH 1 / F-H1)

## Remaining tasks (next safe milestones, not yet started)

1. **F-H2: open-order reconciliation / cancel path.** The kill switch currently
   blocks new submissions only; open orders are preserved because no cancel
   path exists. Needs a safe cancel-or-expire design (respecting duplicate
   prevention and reconciliation) before any live-facing use.
2. **Roadmap G: operational resilience** — structured logging, health/status
   reporting, graceful shutdown, emergency kill-switch execution, restart
   recovery beyond what `recovery.py` already provides.
3. **Peak-equity reset policy.** Once a 2% drawdown kill has tripped, the
   persisted peak stays at its high-water mark; after recovery the operator
   must explicitly reset `paper_reference_equity` (no automatic reset — that
   would weaken the invariant). Document the operator procedure or add an
   explicit, tested reset command. Decision needed.
4. **Roadmap E: exchange event handling** (user-data stream) with
   deterministic reconnect/retry; reconcile local vs exchange state.
5. **Roadmap H final audit** — secret scan, default review, changelog.

## Known limitations

- Live trading remains disabled (`dry_run: true`, `allow_live_execution: false`,
  `main()` raises if dry_run is false). No withdrawal permission anywhere.
- `open_orders` reconciliation is still advisory: `open_orders_available_gate`
  blocks the plan whenever open-order state cannot be VERIFIED (UNKNOWN status).
- The 15m lower-boundary stop and range-break kill remain fail-closed; the
  persisted peak is used only for the 2% equity drawdown gate.
- Grid invariants unchanged: 0.30% hard min net, 0.60% gross step, 2% drawdown
  kill, strict range protection, risk-engine veto over every order.

## Blocked decisions (require human authorization)

- None in this run. (Items above are follow-ups, not blockers.) Enabling live
  trading, loosening any risk parameter, or adding withdrawal permission
  would each require explicit human sign-off per AGENTS.md and were NOT done.
