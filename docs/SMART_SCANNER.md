# RiskPilot Smart Scanner — additive deadline-safe patch

This patch intentionally leaves the existing RiskPilot flow untouched.

It does **not** modify:

- `src/spotguard/config.py`
- `src/spotguard/market.py`
- `src/spotguard/service.py`
- `src/spotguard/live_execution.py`
- `config.json`
- the old `spotguard-monitor.timer`
- LIVE arming, risk policy, approval, execution, TP/SL, partial exit, or reconciliation

The new scanner is a parallel **read-only discovery/ranking layer**.

## Verified deployment status

The Smart Scanner is enabled on the RiskPilot VPS through the user-level
`riskpilot-smart-scanner.timer`.  Its first enabled cycle completed with
`status=0/SUCCESS`; the timer then schedules the next cycle approximately five
minutes later (plus its configured randomized delay).

Observed healthy cycles have covered roughly 480 eligible Spot/USDT symbols,
kept a 64-symbol watchlist and a 48-symbol active universe, and reported
`used_weight_1m` in the 116–196 range out of Binance's 6000/minute budget.
Those values are observations, not static guarantees: the scanner adapts its
active universe and stops kline work when its own lower safety thresholds are
reached.

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
    +-- alert top qualifying radar candidates
    |
    +-- if a top candidate is ALREADY in existing config:
           hand only that candidate to the existing SpotGuard.scan() flow
               -> normal RiskPilot candidate notification
               -> native /binance_spotguard review CANDIDATE_ID button
               -> fresh Agent OS review
               -> PAPER proposal only after the existing checks pass
               -> a separate PAPER approval remains required for a simulated fill
```

Dynamic coins are **never automatically added to `market.symbols`**. This is deliberate. A newly discovered HYPE coin can be surfaced in its first hour, but it remains `RADAR ONLY` until you explicitly decide to expand the execution allowlist later.

The scanner never directly creates a LIVE order, changes LIVE arming, expands
the execution allowlist, or bypasses RiskPilot's fresh-price, approval,
TP/SL, or risk validation. A candidate may also be withheld by the existing
per-symbol cooldown/deduplication guard even when it has a qualifying scanner
score.

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
  alerts.json
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

This performs market reads and scoring but does not send Smart Radar Telegram alerts because `--notify` is absent.

Inspect:

```bash
cat .riskpilot-state/smart-scanner/active-universe.json | python3 -m json.tool
```

If your production `state_dir` is elsewhere, use the path from your existing `config.json`.

## Telegram test

```bash
python3 scripts/riskpilot-smart-scanner.py \
  --smart-config smart-scanner.json \
  --notify --json
```

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
