# Patch notes

## Scope

The original patch was scoped as an additive Smart Scanner / Smart Radar change
and did not replace existing source files. “Additive-only” describes that
original patch scope; it is not a claim about the current integrated repository.

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
