# VPS installation notes

This optional deployment note uses placeholders so it can be published safely. Do not rename an existing VPS repository directory or installed service unit.

Set the existing checkout path locally:

```bash
export RISKPILOT_DIR=/path/to/existing/spotguard-agent-os
cd "$RISKPILOT_DIR"
```

The checkout currently retains the repository directory, systemd unit names, state directory, SQLite filename, and skill path for compatibility. The public command is `riskpilot`; `spotguard` remains an equivalent compatibility alias.

## Install

```bash
./scripts/install.sh
./riskpilot --json check
```

The installer creates `config.json` only when absent, keeps it private, and does not overwrite an existing configuration. Use `config.example.json` as a placeholder template; never commit `config.json`.

## Agent OS read

Codex CLI and Binance Agent OS OAuth are optional for the PAPER demo. Grant market-data scope only. Do not grant Futures, Margin, Transfer, wallet, payment, borrowing, or withdrawal permissions.

```bash
./scripts/configure-codex-agent-os.sh
./riskpilot --json agent-os status
./riskpilot --json agent-os market --symbol BTCUSDT
```

RiskPilot invokes Agent OS only for an explicit read-only confirmation. The scheduled scanner remains deterministic by design but is currently disabled during Binance public-REST rate-limit/IP-ban optimization.

## Verification and demo

```bash
./scripts/verify.sh
./scripts/demo-track-a.sh
```

The demo uses temporary fixture/PAPER state, never sends Telegram, and never performs a Binance write. Live execution is disabled and disarmed.

## Timer

Inspect an already installed timer without changing it:

```bash
systemctl --user list-timers --all
journalctl --user -u spotguard-monitor.service -n 50 --no-pager
```

The retained `spotguard-monitor.service` and `spotguard-monitor.timer` names are compatibility identifiers. The timer is intentionally disabled until request-budget/backoff optimization is complete; do not enable it as part of repository review.
