# Evaluation evidence

RiskPilot deliberately separates **offline reproducibility** from **authenticated LIVE evidence**.

Run the complete offline evidence set with `./scripts/verify.sh` and the repeatable walkthrough with `./scripts/demo-track-a.sh`. These paths require no Binance OAuth credentials and never place a real order.

The real-funds demo and the repository-to-video evidence bridge are documented in [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md).

| Capability | Implementation file | Test proving it | Safe reproduction | Expected output |
|---|---|---|---|---|
| Scanner implementation | `src/spotguard/service.py` | `test_symbol_expansion.py` | `./riskpilot --config /path/to/sanitized-config.json --json scan --synthetic --dry-run` | offline configured-symbol scan; VPS timer/deployment status is not asserted by repository tests |
| Agent OS read-only confirmation | `src/spotguard/codex_bridge.py` | `test_codex_bridge.py` | `./riskpilot --json agent-os status` | read-only boundary |
| Signal generation | `src/spotguard/strategy.py` | `test_indicators.py` | `./scripts/demo-track-a.sh` | deterministic candidate |
| PAPER proposal | `src/spotguard/service.py` | `test_manual_flows.py` | `./scripts/demo-track-a.sh` | pending proposal and simulated fill |
| Telegram approval | `src/spotguard/security.py`, `src/spotguard/db.py` | `test_durable_approval.py` | `PYTHONPATH=src python3 -m unittest tests.test_durable_approval` | owner-bound single use |
| Scale-in and invariant | `src/spotguard/service.py`, `src/spotguard/paper.py` | `test_paper_bracket_invariant.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_bracket_invariant` | valid aggregate bracket |
| Partial close | `src/spotguard/service.py`, `src/spotguard/db.py` | `test_paper_limits_partial_close.py` | `./scripts/demo-track-a.sh` | aggregate quantity reduced |
| Automatic TP/SL | `src/spotguard/service.py` | `test_paper_positions.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_positions` | atomic PAPER exit |
| Position and balance accounting | `src/spotguard/db.py` | `test_paper_positions.py` | `./riskpilot --config /path/to/sanitized-config.json --json paper balance` | reconciled totals |
| Risk rejection | `src/spotguard/service.py` | `test_paper_execution_recovery.py` | `./scripts/demo-track-a.sh` | unsafe request rejected |
| Daily quota | `src/spotguard/db.py`, `src/spotguard/service.py` | `test_paper_limits_partial_close.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_limits_partial_close` | 10 accepted, 11th rejected |
| Tranche/position limits | `src/spotguard/service.py` | `test_paper_limits_partial_close.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_limits_partial_close` | 10 tranches / 5 positions |
| Approval replay rejection | `src/spotguard/db.py` | `test_approval_flow.py` | `PYTHONPATH=src python3 -m unittest tests.test_approval_flow` | replay rejected |
| Timeout and restart recovery | `src/spotguard/db.py`, `src/spotguard/service.py` | `test_paper_execution_recovery.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_execution_recovery` | no duplicate |
| LIVE protected entry/exit safety | `src/spotguard/live_execution.py` | `test_live_safety.py` | `PYTHONPATH=src python3 -m unittest tests.test_live_safety` | fixed Spot write shapes, response validation, cancel/sell/re-arm ordering, and fail-closed ambiguous-result behavior using mocked transport |

## What the offline suite proves

The offline suite proves code-level invariants without secrets or real-money side effects. In particular, `test_live_safety.py` exercises protected request construction and mocked LIVE workflow responses; it does not pretend to be an authenticated Binance integration test.

## What the public LIVE demo adds

The public demo separately shows an authenticated real-funds Spot lifecycle: real account state, protected entry, partial exit, protection re-arm, full exit, and Binance.com out-of-band history verification.

See [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md) for the public order identifiers, implementation mapping, evidence-manifest hash, and explicit limitations.
