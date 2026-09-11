# LIVE marketable LIMIT + hard slippage cap patch

This patch keeps RiskPilot Spot-only, manual-approval, OTOCO protection, and all existing capital/risk ceilings unchanged.

## Behavior

- At proposal creation, the observed Binance ask is stored as `entry_market_ask`.
- The approved working BUY LIMIT is `floor_to_tick(ask * (1 + entry_slippage_cap_pct / 100))`.
- The default hard cap is `0.20%` and is configurable at `live.entry_slippage_cap_pct` (accepted range `0..1.0`).
- At execution, RiskPilot performs a fresh ask read. If the fresh ask is above the approved LIMIT/cap, it fails before the MCP write and requires a requote.
- The OTOCO request asks Binance for `newOrderRespType=FULL`. When the working LIMIT fills immediately and Binance/MCP returns full order reports, RiskPilot can persist `executedQty`, weighted fill price, `fills[]`, `commission`, and `commissionAsset` directly from the first response.
- The delegated OTOCO schema must explicitly expose `newOrderRespType`; otherwise LIVE readiness fails closed instead of sending an unverified request shape.
- A BUY fill price above the immutable approved cap is never accepted as verified accounting evidence.

## Safety note

The cap bounds price; it does not guarantee an immediate fill. Network/market movement after the final fresh read may leave the LIMIT resting. Existing reconciliation / WAITING_FOR_FILL behavior remains fail-closed and writes are never blindly retried.
