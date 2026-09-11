# Public LIVE execution evidence

RiskPilot keeps two evidence paths separate on purpose:

- **Offline reproducibility:** `./scripts/verify.sh` and `./scripts/demo-track-a.sh` run without Binance credentials and never place a real order.
- **Authenticated LIVE evidence:** the public demo shows a real-funds Binance Agentic Spot lifecycle using the protected LIVE path implemented in `src/spotguard/live_execution.py`.

This separation is intentional. CI must not require private OAuth material or spend real funds.

## Public demo

- YouTube: https://youtu.be/aYC23eYYUx0
- X submission: https://x.com/bobbymarc00/status/2097039814482878806

The video shows the user-controlled sequence:

1. Read LIVE Spot balance and open-position state.
2. Analyze and rank XRP, BNB, and SOL.
3. No autonomous candidate qualifies.
4. The user intentionally requests a manual LIVE XRP trade to demonstrate the protected execution path.
5. Approve and execute the protected Spot entry.
6. Keep TP/SL protection active.
7. Partially exit 47% and re-arm the same approved protection for the remainder.
8. Fully exit the remaining XRP.
9. Verify the resulting transactions in Binance.com as an out-of-band check.

## Public execution identifiers shown in the demo

| Step | Symbol | Public Binance order ID |
|---|---|---:|
| Protected entry | XRPUSDT | `15398042241` |
| 47% partial exit | XRPUSDT | `15398044500` |
| Full remaining exit | XRPUSDT | `15398046966` |

These identifiers make the video, repository claims, and submission narrative refer to the same demonstrated lifecycle. They are not credentials and do not authorize account access.

## Evidence manifest

Canonical manifest:

```json
{"demo":"https://youtu.be/aYC23eYYUx0","entry_order_id":15398042241,"full_exit_order_id":15398046966,"partial_exit_order_id":15398044500,"symbol":"XRPUSDT","x_submission":"https://x.com/bobbymarc00/status/2097039814482878806"}
```

SHA-256:

```text
a9b71d25f0558d86cdedfae607511734529801a93d983a6d3e0d4e9bd4b06cf3
```

This hash only identifies the public evidence manifest above. It is **not** a Binance signature, exchange attestation, or cryptographic proof that a particular video binary was produced by a particular Git commit.

## Repository path corresponding to the demonstrated behavior

The current public implementation contains the authenticated LIVE transport and fixed Spot write shapes used by RiskPilot:

- `src/spotguard/live_execution.py`
  - direct OAuth-authenticated Binance Agent OS/MCP `tools/call` transport;
  - protected `spot.orderListOtoco` entry construction with `newOrderRespType=FULL`;
  - exact protected-OCO cancellation;
  - approved `spot.newOrder` MARKET SELL exit;
  - SELL OCO protection restore;
  - ordered partial-exit `cancel -> verify -> sell -> re-arm` workflow;
  - response and protective-leg validation.
- `src/spotguard/service.py`
  - immutable proposal/approval path;
  - marketable BUY LIMIT pricing with an immutable ask-relative hard slippage cap and pre-write fresh-ask rejection;
  - exchange fill/commission provenance capture and cap verification;
  - execution lease handling;
  - ambiguous write outcomes transition to `RECONCILE` instead of blind retry.
- `tests/test_live_safety.py`
  - exact OTOCO request-shape tests;
  - malformed/ambiguous response rejection;
  - protected partial-exit cancel/sell/re-arm ordering;
  - no-sell behavior when protection cancellation remains ambiguous;
  - open-order read-back check for ambiguous OCO cancellation.

Protected LIVE entry was publicly added before the demo, and protected LIVE exit/re-arm support is present from commit `b373d09178aae8b10fe5bbc5770d86ff5e017550` onward. The final public submission documentation was completed in commit `61bc6c86fd37fe10580745c4dac3c38b5095871b`.

This document does not claim a cryptographic attestation that the running process captured in the video exactly equals one Git commit. It provides a transparent bridge between the public demo, the public identifiers visible in that demo, and the implementation/tests available for inspection.

## Reconciliation boundary

RiskPilot distinguishes **targeted read-back reconciliation** from **generic automatic reconciliation**.

Implemented targeted behavior includes checking active Spot orders after an ambiguous protected-OCO cancellation before allowing a partial sell to continue.

For a financial write whose final exchange outcome cannot be established safely, the service records `RECONCILE` and does not automatically retry. Generic automatic recovery of every ambiguous Binance write is intentionally unavailable in this release; an operator must inspect exchange state before any further action.

This is a limitation, but it is also a safety boundary: an unknown result is never treated as proof that Binance rejected the first request.

## Credential and CI boundary

OAuth credentials live only in the dedicated local execution profile/runtime credential store. They are not committed to this repository.

The offline verification/CI path is designed to validate deterministic request construction, policy boundaries, state transitions, and mocked LIVE response handling without making authenticated financial writes. The authenticated real-funds path is evidenced separately by the public demo above.
