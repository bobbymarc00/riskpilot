# RiskPilot Phase 1: scalable equity-aware guardrails

Phase 1 makes current account equity the primary source for capital and risk
limits. It scales down for a small account and up for a large account without a
source edit. The previous USD values remain available only as a backward-
compatible legacy policy or an explicitly enabled emergency backstop.

This phase does not make reward:risk adjustable. The existing structural stop,
minimum R:R rejection, TP/SL construction, exit protection, Spot-only boundary,
human approval, immutable proposal, and LIVE enable/arm/readiness gates remain
unchanged.

## Versioned configuration

Root config versions 1 and 2 are accepted. If `sizing_policy` is absent,
RiskPilot follows the legacy absolute limits exactly and does not add account
mark-price reads merely because the code was upgraded. Schema 1
`linear_to_hard_cap` remains readable for migration. New installations use
schema 2:

```json
{
  "sizing_policy": {
    "enabled": true,
    "schema_version": 2,
    "capital_basis": "mark_to_market_equity",
    "quote_asset": "USDT",
    "reference_equity_usdt": 1000,
    "scaling_model": "equity_percentage_risk"
  },
  "risk": {
    "risk_per_trade_pct": 0.005,
    "max_aggregate_open_risk_pct": 0.015,
    "daily_realized_loss_pct": 0.02,
    "weekly_realized_loss_pct": 0.05
  },
  "capital": {
    "max_position_pct": 0.20,
    "max_total_exposure_pct": 0.60,
    "min_free_reserve_pct": 0.20
  },
  "operations": {
    "max_open_positions": 5,
    "max_pending_live_proposals": 1
  },
  "execution": {
    "max_equity_drift_pct": 0.02
  },
  "absolute_safety_caps": {
    "enabled": false
  }
}
```

Percentage values are decimal fractions: `0.005` means 0.5%. Risk, capital,
operations, and execution settings are deliberately separate. A missing,
partial, non-finite, negative, inconsistent, or mismatched schema fails config
loading closed.

`reference_equity_usdt` is an explicit audit/migration anchor. If it is absent
from an early schema-2 config, the loader safely derives it from the existing
`paper.initial_balance_usdt`. It is not a sizing denominator or upper bound in
schema 2; all primary limits below use current effective equity directly.

The old USD settings under `risk`, `paper`, and `live` are retained. With
`absolute_safety_caps.enabled: false`, they do not prevent schema 2 from scaling
up. With it set to `true`, every percentage-derived maximum is clamped with the
corresponding old value:

```text
effective maximum = min(equity × configured percentage, old absolute cap)
```

The backstop is optional and disabled in the example configuration.
It clamps maximum allowances only; it does not activate the legacy 8 USDT
reserve floor. Schema-2 reserve remains `effective equity × min_free_reserve_pct`.

## Equity and available capital

For Spot and quote asset `Q`:

```text
equity = free Q + locked Q + Σ(base quantity × current bid mark)
required reserve = effective equity × min_free_reserve_pct
available capital after reserve = max(0, free Q - required reserve)
```

Open Spot positions remain part of mark-to-market equity. They also remain in
total exposure and aggregate stop-risk. Their current value is never treated as
free USDT: only the actual free quote balance can fund a BUY. Unvalued assets,
invalid balances, incomplete protection evidence, or missing marks reject the
entry.

PAPER exposure uses committed quote cost. LIVE exposure uses marked Spot value.
PAPER stop-risk includes its existing fee/slippage model. LIVE open risk is
quantity times the distance between the audited entry basis and protective
stop. An open LIVE balance without matching, complete Spot OCO evidence blocks
new exposure pending reconciliation.

## Effective percentage limits

For effective equity `E`:

```text
max position notional       = E × max_position_pct
max total exposure          = E × max_total_exposure_pct
risk budget per position    = E × risk_per_trade_pct
max aggregate open risk     = E × max_aggregate_open_risk_pct
daily realized-loss limit   = E × daily_realized_loss_pct
weekly realized-loss limit  = E × weekly_realized_loss_pct
reserve                     = E × min_free_reserve_pct
```

With the default profile:

| Equity | Position | Exposure | Risk/trade | Aggregate risk | Daily | Weekly | Reserve |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 30 | 6 | 18 | 0.15 | 0.45 | 0.60 | 1.50 | 6 |
| 100 | 20 | 60 | 0.50 | 1.50 | 2 | 5 | 20 |
| 1,000 | 200 | 600 | 5 | 15 | 20 | 50 | 200 |
| 20,000 | 4,000 | 12,000 | 100 | 300 | 400 | 1,000 | 4,000 |

`max_open_positions` is an integer operational limit and is not multiplied by
equity.

## Structural-stop position sizing

The sequence is signal → entry → structural stop → risk budget → position size.
The Smart Scanner supplies market discovery and entry/stop/target context, but
cannot allocate capital. The risk engine always has veto power.

```text
stop fraction = abs(entry - stop) / entry
risk-derived notional = risk budget / stop fraction
remaining exposure = max total exposure - current exposure
remaining open risk = max aggregate risk - current open risk
capital capacity = available buying power / (1 + fee-buffer rate)

safe notional = min(
  risk-derived notional,
  max position notional,
  remaining position capacity,
  remaining exposure,
  remaining-risk capacity / stop fraction,
  capital capacity
)
```

The risk budget is a maximum, not a target. An explicit manual amount is either
accepted exactly or rejected; it is never silently reduced. A scanner proposal
with no explicit amount receives the risk-calculated amount before the proposal
is persisted. From that point the amount and quantity are immutable.

Binance `MIN_NOTIONAL`, LOT_SIZE, tick size, and symbol status remain exchange
filters, not policy caps. If Binance requires 5 while the safe calculated size
is 3, RiskPilot rejects with
`MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE`; it never raises 3 to 5.

## Immutable proposal and execution revalidation

Every BUY proposal embeds a signed/canonical `policy_snapshot` containing:

- policy schema/version, capture time, and config fingerprint;
- free/locked quote, marked Spot assets, equity, reserve, and buying power;
- current exposure, open risk, daily/weekly realized loss, positions/tranches;
- effective limits and remaining capacities;
- entry, structural stop, stop fraction, risk budget, risk-derived and selected
  notional, calculated quantity, expected risk, and fee buffer;
- structured reason codes and explanations.

Approval and execution each refresh account, market, exchange-filter, exposure,
risk, and loss state. Execution limits use:

```text
effective equity = min(equity at proposal, current equity)
```

An equity increase therefore cannot enlarge an old approval. A decrease larger
than `execution.max_equity_drift_pct` invalidates it with
`EQUITY_DRIFT_EXCEEDED`. A smaller move does not automatically invalidate it,
but the exact proposal must still pass every refreshed limit. Any failure makes
the proposal terminal and requires a new proposal; entry, size, SL, TP, R:R, and
snapshot are never rewritten after approval.

Core reason codes include:

```text
MAX_POSITION_EXCEEDED
MAX_TOTAL_EXPOSURE_EXCEEDED
MAX_AGGREGATE_RISK_EXCEEDED
DAILY_LOSS_LIMIT_REACHED
WEEKLY_LOSS_LIMIT_REACHED
INSUFFICIENT_FREE_BALANCE
MIN_FREE_RESERVE_VIOLATION
MAX_OPEN_POSITIONS_REACHED
MIN_NOTIONAL_EXCEEDS_RISK_DERIVED_SIZE
EXCHANGE_FILTER_FAILED
PROPOSAL_EXPIRED
APPROVAL_EXPIRED
APPROVAL_INVALID
ACCOUNT_STATE_CHANGED
EQUITY_DRIFT_EXCEEDED
REVALIDATION_FAILED
LIVE_NOT_ENABLED
LIVE_NOT_ARMED
```

## Read-only explanation

```bash
riskpilot --config config.json --json policy explain
riskpilot --config config.json --json policy explain --mode paper \
  --symbol BTC --quote-amount 6 --risk-at-stop 0.10
```

The command reports equity separately from buying power, percentage and legacy
backstop settings, effective limits, usage, remaining position/exposure/risk,
and structured rejection reasons. It creates no candidate or proposal, changes
no LIVE state, and submits no write.

## Phase 2 R:R extension

Phase 1 keeps `risk.min_reward_risk` and the established bracket/exit code
unchanged. The policy snapshot reserves an `extensions` object. Phase 2 can add
a versioned R:R descriptor and evaluated R:R evidence there, then validate it
beside the existing fingerprint and immutable bracket. Equity readers, sizing,
proposal persistence, approval revalidation, and execution revalidation do not
need a structural refactor. No Telegram, UI, or user-adjustable R:R setting is
implemented in Phase 1.
