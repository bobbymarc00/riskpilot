# Evaluation evidence

Run the complete offline evidence set with `./scripts/verify.sh` and the repeatable walkthrough with `./scripts/demo-track-a.sh`.

| Capability | Implementation file | Test proving it | Safe reproduction | Expected output |
|---|---|---|---|---|
| Scheduled scan | `src/spotguard/service.py` | `test_symbol_expansion.py` | `./riskpilot --config /path/to/sanitized-config.json --json scan --synthetic --dry-run` | configured symbols scanned |
| Agent OS read-only confirmation | `src/spotguard/codex_bridge.py` | `test_codex_bridge.py` | `./riskpilot --json agent-os status` | read-only boundary |
| Signal generation | `src/spotguard/strategy.py` | `test_indicators.py` | `./scripts/demo-track-a.sh` | deterministic candidate |
| PAPER proposal | `src/spotguard/service.py` | `test_manual_flows.py` | `./scripts/demo-track-a.sh` | pending proposal and fill |
| Telegram approval | `src/spotguard/security.py`, `src/spotguard/db.py` | `test_durable_approval.py` | `PYTHONPATH=src python3 -m unittest tests.test_durable_approval` | owner-bound single use |
| Scale-in and invariant | `src/spotguard/service.py`, `src/spotguard/paper.py` | `test_paper_bracket_invariant.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_bracket_invariant` | valid aggregate bracket |
| Partial close | `src/spotguard/service.py`, `src/spotguard/db.py` | `test_paper_limits_partial_close.py` | `./scripts/demo-track-a.sh` | aggregate quantity reduced |
| Automatic TP/SL | `src/spotguard/service.py` | `test_paper_positions.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_positions` | atomic exit |
| Position and balance accounting | `src/spotguard/db.py` | `test_paper_positions.py` | `./riskpilot --config /path/to/sanitized-config.json --json paper balance` | reconciled totals |
| Risk rejection | `src/spotguard/service.py` | `test_paper_execution_recovery.py` | `./scripts/demo-track-a.sh` | unsafe request rejected |
| Daily quota | `src/spotguard/db.py`, `src/spotguard/service.py` | `test_paper_limits_partial_close.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_limits_partial_close` | 10 accepted, 11th rejected |
| Tranche/position limits | `src/spotguard/service.py` | `test_paper_limits_partial_close.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_limits_partial_close` | 10 tranches / 5 positions |
| Approval replay rejection | `src/spotguard/db.py` | `test_approval_flow.py` | `PYTHONPATH=src python3 -m unittest tests.test_approval_flow` | replay rejected |
| Timeout and restart recovery | `src/spotguard/db.py`, `src/spotguard/service.py` | `test_paper_execution_recovery.py` | `PYTHONPATH=src python3 -m unittest tests.test_paper_execution_recovery` | no duplicate |
| LIVE fail-closed readiness | `src/spotguard/live_execution.py` | `test_live_safety.py` | `./riskpilot --config /path/to/sanitized-config.json --json live status` | disabled, disarmed, not ready |
