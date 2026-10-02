# HERMES AUTONOMOUS TASK QUEUE

Repository: adaptive-grid

Read `AGENTS.md` first.

## Mission

Audit and improve the repository autonomously. Capital protection and deterministic behavior have highest priority.

## First run

1. Run `git status`.
2. Inspect the repository tree.
3. Read README and Python modules.
4. Read all tests.
5. Run the existing test suite and record the baseline.
6. Compare implementation against documented requirements.
7. Write a concise audit note before major changes.

## Priority

1. Correctness bugs.
2. Risk-control gaps.
3. Exchange filter and fee handling.
4. State persistence and reconciliation.
5. Missing execution components, only after safety prerequisites.
6. Tests for every new behavior.
7. Documentation and diagnostics.

## Rules

- Do not enable live trading.
- Do not commit secrets.
- Do not delete tests to make them pass.
- Do not weaken risk controls.
- Do not push to `main`.
- Commit coherent milestones on the autonomous branch.
- After meaningful changes: targeted tests -> full `pytest -q` -> git diff review.
- If a task requires human authorization, document it and stop that task.

## Run completion report

Leave:
- test result
- summary of changes
- commit hashes
- remaining tasks
- known limitations
- blocked decisions

Do not claim completion while safety-critical tests fail.
