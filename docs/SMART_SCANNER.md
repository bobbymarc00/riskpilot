# RiskPilot Smart Scanner

The scanner remains a parallel **read-only discovery/ranking layer**: it never
adds symbols to the execution allowlist or submits an order. The original
scanner patch was described as “additive-only”; that description is historical
and no longer describes the integrated repository. Subsequent integration work
updated RiskPilot service, policy, intent, Telegram, CLI, and tests so the
scanner can persist radar state and hand configured symbols into the existing
candidate flow. The safety boundary remains unchanged: discovery is not a
candidate, proposal, or LIVE capability.

## Deployment status

This document describes the scanner implementation and operating model. It
does not assert whether a VPS timer is currently enabled, disabled, or running;
that state must be checked on the target host.

## Flow

```text
Every 5 minutes
    |
    +-- one all-market /api/v3/ticker/24hr pulse
    |      -> all Binance symbols in one response
    |      -> filter Spot + USDT + TRADING locally
    |      -> compare with recent local pulse history
    |
    +-- lanes
    |      CORE       liquidity / spread quality
    |      MOMENTUM   fresh move + volume/trade acceleration
    |      HYPE       5m activity burst + emerging/new symbol bonus
    |
    +-- active universe
    |      quiet market   24
    |      normal market  36
    |      hot market     48
    |
    +-- HYPE lane: 1m candles every pulse
    |      <60 closed 1m candles -> RADAR ONLY, still visible
    |
    +-- CORE/MOMENTUM: existing 15m interval only once per 15m bucket
    |
    +-- deterministic RiskPilot scoring where enough candles exist
    |
    +-- persist a cross-lane Top Radar Potensi snapshot (read-only)
    |      -> no periodic Telegram notification
    |      -> available through `riskpilot radar` or trusted `top radar` / `radar potensi`
    |
    +-- if a top candidate is ALREADY in existing config:
           hand only that candidate to the existing SpotGuard.scan() flow
               -> normal RiskPilot candidate notification
               -> native /binance_spotguard review CANDIDATE_ID button
               -> isolated AI REVIEW with a minimal candidate payload and fresh Agent OS spot.klines read
               -> approval-gated proposal only after structured APPROVE
               -> PAPER remains simulated; LIVE remains disarmed and fail-closed
                  until its existing readiness/arm checks pass
```

Dynamic coins are **never automatically added to `market.symbols`**. This is deliberate. A newly discovered HYPE coin can be surfaced in its first hour, but it remains `RADAR ONLY` until you explicitly decide to expand the execution allowlist later.

## Notifications and Top Radar Potensi

The five-minute scanner no longer sends passive `RISKPILOT SMART RADAR` texts.
It persists `top-radar.json` and only the existing candidate handoff can send a
Telegram notification: `RISKPILOT CANDIDATE` with AI REVIEW, followed by the
normal approval-gated proposal flow. Existing safety/error notifications remain
separate from this passive-observation suppression.

Use the read-only local view when needed:

```bash
./riskpilot --json radar --limit 5
```

In a trusted Telegram direct chat, `top radar`, `radar potensi`, or `/spot
radar` routes to the same local snapshot. The ranking blends CORE, MOMENTUM,
and HYPE; a HYPE pulse is not automatically a trade signal. `POTENSI_TINGGI`
means the current scan has a qualifying closed 15m canonical score, while
`EMERGING` is an eligible 1m observation. Neither is a candidate, proposal, or
entry instruction. AI REVIEW remains available only after the existing 15m
candidate flow has independently passed its validation.

The scanner never directly creates a LIVE order, changes LIVE arming, expands
the execution allowlist, or bypasses RiskPilot's fresh-price, approval,
TP/SL, or risk validation. A candidate may also be withheld by the existing
per-symbol cooldown/deduplication guard even when it has a qualifying scanner
score.

AI REVIEW is stateless and never reuses Telegram, Bob Agent, or a general
assistant session. Its subprocess is ephemeral, uses the dedicated Agent OS
profile, receives only candidate indicators/reasons/provenance and required
risk facts, and has one read-only `spot.klines` tool available. Invalid/empty
output, timeout, tool failure, stale or mismatched candles, and `REJECT`/
`NO_TRADE` all fail closed. An empty final response may be retried once in a
new ephemeral process; there is no full-session fallback. Review telemetry is
stored as redacted local events (token estimates, latency, decision, failure
category, and retry flag); secrets and raw prompts are excluded.

The logged `operator.read`/system-presence warning is not part of the review
contract. It belongs to an unrelated operator/dashboard capability check;
`spot.klines` remains the sole required Agent OS read for this flow, so review
does not broaden permissions to satisfy that warning.

`scheduled_proposal_mode` controls whether a successfully reviewed scheduled
candidate becomes a PAPER or LIVE proposal. It does not arm LIVE and it never
submits an order by itself: the existing native LIVE approval, account
readiness, fresh validation, and protected Spot order checks still apply.

## Rate safety

Internal guardrails are intentionally far below Binance's public limit:

```text
0-300 used weight/min   GREEN
300-600                 cap expansion
600-750                 active universe forced down to 24
>=750                   no kline scanner requests
429                     circuit cooldown; Retry-After when available
418                     smart scanner persistently disabled
```

The design leaves at least ~85% of the documented 6000/min capacity outside the scanner's budget. A single all-market 24h ticker request currently costs 80 request weight. At a 5-minute cadence this averages about 16 weight/min before kline work.

`circuit.json` is a fail-closed local circuit breaker. A `429` records a
cooldown and skips further scanner work until it expires. A Binance `418` IP
ban persistently disables Smart Scanner runs until an operator investigates and
clears the circuit deliberately; it does not auto-retry around a ban.

## Local state

All smart-scanner state is under the existing RiskPilot state directory:

```text
<state_dir>/smart-scanner/
  active-universe.json
  baseline-24h.json
  smart-exchange-info.json
  pulse-history.json
  top-radar.json
  circuit.json
  schedule.json
  scanner.lock
```

Deleting this directory resets only Smart Scanner state; it does not delete the RiskPilot ledger.

## First test — no Telegram

From the repo root:

```bash
cp smart-scanner.example.json smart-scanner.json
chmod 600 smart-scanner.json
python3 scripts/riskpilot-smart-scanner.py --smart-config smart-scanner.json --json
```

This performs market reads and scoring without sending Telegram notifications.

Inspect:

```bash
cat .riskpilot-state/smart-scanner/active-universe.json | python3 -m json.tool
```

If your production `state_dir` is elsewhere, use the path from your existing `config.json`.

## Candidate/proposal notification test

```bash
python3 scripts/riskpilot-smart-scanner.py \
  --smart-config smart-scanner.json \
  --notify --json
```

`--notify` now enables only the existing configured-symbol candidate/proposal
handoff. It never sends passive Radar texts.

## Enable timer

Only after the two manual tests above succeed:

```bash
./scripts/install-smart-scanner.sh
```

The installer enables **only** `riskpilot-smart-scanner.timer`. It does not enable or change `spotguard-monitor.timer`.

The source service defines a bounded systemd PATH that includes
`%h/.npm-global/bin` and `%h/.local/bin` before the standard system paths, so
the user-installed `openclaw` command is available to the scanner. On an
already-installed VPS, retain any working local PATH drop-in until this source
unit has been reinstalled and `systemctl --user daemon-reload` has completed.

Check it:

```bash
systemctl --user list-timers --all | grep riskpilot
systemctl --user status riskpilot-smart-scanner.timer --no-pager
journalctl --user -u riskpilot-smart-scanner.service -n 100 --no-pager
```

## Emergency stop

```bash
systemctl --user disable --now riskpilot-smart-scanner.timer
```

or:

```bash
./scripts/remove-smart-scanner.sh
```

## Recovering from a false/stale circuit after investigation

Do not clear a circuit while Binance is actually returning 418/429. Once the cause is known and the ban/cooldown has expired:

```bash
systemctl --user disable --now riskpilot-smart-scanner.timer
rm -f .riskpilot-state/smart-scanner/circuit.json
python3 scripts/riskpilot-smart-scanner.py --smart-config smart-scanner.json --json
```

Then re-enable only if the manual run is healthy.
