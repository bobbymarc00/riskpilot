# Audit checkpoints — post-v1.0.0

`v1.0.0` is a frozen hackathon baseline. These checkpoints describe changes
prepared for the next patch release and never require moving that tag.

| Checkpoint | Status | Evidence |
| --- | --- | --- |
| CI workflow parses and creates jobs | Fixed locally; remote rerun required | Removed duplicate case-insensitive `NO_PROXY` / `no_proxy` workflow variables. GitHub had rejected the workflow before any job was created. |
| LIVE entry limits | Implemented fail-closed | Manual and scheduled entries take account, OCO/open-order, and bounded trade-history snapshots at creation, claim, and execution. Incomplete protection, history, or cost basis rejects a new entry. |
| Partial-fill exits | Fixed | Only `FILLED` permits completion or OCO re-arm. `NEW` and `PARTIALLY_FILLED` enter reconciliation with no retry. |
| Legacy PAPER migration | Fixed | Ambiguous historic fills raise `LedgerError`, with a startup regression test. |
| LIVE exchange metadata | Fixed | LIVE symbol validation accepts at most a five-minute `exchangeInfo` cache; stale refresh failures reject LIVE. |
| Readiness reporting | Fixed, intentionally restrictive | Profile setup is reported separately from verified account/read/write capabilities. Unprobed write scope/schema cannot make `execution_ready` true. |
| Runtime/package version | Fixed | Runtime and package metadata are both `1.0.1`. |
| Smart Scanner integrity/docs | Fixed | Manifest hashes validate; documentation no longer calls the integrated scanner patch additive-only. |

## Release gates still outside this working tree

1. Commit and push the patch, then confirm a GitHub Actions run has created and
   passed both Python matrix jobs. The prior public run failed at workflow
   parsing and therefore has no unit-test evidence.
2. Establish an independently auditable Binance capability probe for write
   scope and delegated-tool schema. Until then, `execution_ready` stays false
   by design.
3. Before permitting another LIVE entry after any sale, perform a cost-basis
   reconciliation from complete exchange history. The current policy blocks
   such an entry rather than estimating realized loss from partial history.
