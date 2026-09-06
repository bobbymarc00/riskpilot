# Two-minute demo script

## 0–20s — Product and problem

Show the README opening and say:

> RiskPilot is an Agent OS-powered Spot trading copilot. It separates deterministic scanning, read-only verification, human approval, and PAPER execution so an uncertain model cannot become an execution authority.

## 20–45s — Architecture and Agent OS

Show the Mermaid diagram and run:

```bash
./riskpilot --json agent-os status
./riskpilot --json agent-os market --symbol BTCUSDT
```

Explain that routine scans use public Binance REST, while Agent OS is called only for a qualified candidate and only for a fixed read-only market confirmation.

## 45–90s — Reproducible PAPER workflow

Run `./scripts/demo-track-a.sh`. Point out the deterministic scan, explicit fixture label, proposal, simulated approval, partial close, risk rejection, and audit count. Run it a second time to show fresh temporary state and repeatability.

For a Telegram-connected PAPER walkthrough, use `/spot paper balance`, `buy 25 usd of SOL`, approve the proposal button, then request `close 50% SOL`. Never claim the fixture path is an Agent OS confirmation.

## 90–120s — Safety evidence

Show the test command and live status:

```bash
./scripts/verify.sh
./riskpilot --json live status
```

End by showing the immutable audit evidence and saying:

> LIVE is disabled and disarmed. No real order was placed, no Telegram message was sent by this local demo, and there is no profit guarantee.

## Asset checklist

- Replace the video field with the owner’s real recording URL.
- Crop credentials, IDs, usernames, local paths, and notification metadata.
- Label every fixture and PAPER result.
- Include genuine Agent OS read-only transcript evidence only if available.
