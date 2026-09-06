# Compatibility identifiers

## Agent OS compatibility

The normal profile name is `binance-marketdata`; its generic MCP outer tool is
`tool_execute` and its validated inner target is `spot.klines`. A legacy broad
OAuth profile may be selected only through explicit `legacy_oauth_profile: true`
configuration and is locked to PAPER. A missing explicit profile leaves analysis
unavailable rather than falling back.

RiskPilot is the public product name. The following legacy SpotGuard identifiers remain intentionally unchanged:

- Python package/import path `spotguard` and class names.
- `SPOTGUARD_CONFIG` and `SPOTGUARD_PROJECT_ROOT`.
- State directory and `spotguard.db`.
- SQLite tables/columns, audit events, IDs, and `spotguard.order.v1`.
- Callback prefix `sg:` and its parser, retained only for existing signed payload compatibility. On OpenClaw 2026.8.1 it must not be presented as a working generic-button transport because OpenClaw wraps it in unclaimed `tgcb1` data before RiskPilot can receive it.
- Skill directory/name `binance-spotguard`.
- Systemd unit names and installed repository path.
- `./spotguard` CLI alias.
- Existing databases without presentation-locale rows. They remain valid and use
  the configured fallback locale for old proposals and callbacks.

They protect existing deployments, ledger history, canonical hashes, and pending approvals. New user-facing documentation and notifications use RiskPilot; these names are compatibility identifiers, not public branding.
