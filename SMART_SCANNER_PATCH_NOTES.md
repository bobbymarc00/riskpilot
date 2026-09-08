# Patch notes

## Scope

Additive Smart Scanner / Smart Radar only. No existing source file is replaced.

## New files

- `scripts/riskpilot-smart-scanner.py`
- `scripts/install-smart-scanner.sh`
- `scripts/remove-smart-scanner.sh`
- `systemd/riskpilot-smart-scanner.service`
- `systemd/riskpilot-smart-scanner.timer`
- `smart-scanner.example.json`
- `docs/SMART_SCANNER.md`
- `tests/test_smart_scanner.py`

## Safety boundary

The smart scanner may discover and score symbols outside the current RiskPilot `market.symbols` list, but it never adds them to the execution allowlist. Only an already-configured symbol can be handed to the existing `SpotGuard.scan()` path.

This keeps the deadline-critical LIVE path unchanged.
