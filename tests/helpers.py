from __future__ import annotations

import json
from pathlib import Path


def config_dict(root: Path, mode: str = "paper") -> dict:
    profile = root / "dedicated-codex"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.toml").write_text("# test-only dedicated profile\n", encoding="utf-8")
    (profile / "workspace").mkdir(exist_ok=True)
    return {
        "version": 1,
        "presentation": {"default_locale": "en"},
        "mode": mode,
        "scheduled_proposal_mode": "paper",
        "workspace": str(root / "workspace"),
        "state_dir": str(root / "state"),
        "telegram": {
            "enabled": False,
            "chat_id": "123456789",
            "channel": "telegram",
            "account": None,
        },
        "market": {
            "base_url": "https://api.binance.com",
            "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
            "interval": "15m",
            "lookback": 120,
            "request_timeout_seconds": 5,
            "min_signal_score": 70,
            "candidate_ttl_minutes": 30,
            "candidate_cooldown_minutes": 60,
            "max_entry_drift_pct": 1.0,
            "max_spread_pct": 0.25,
        },
        "risk": {
            "quote_asset": "USDT",
            "default_order_size_usdt": 6.0,
            "min_quote_amount": 5.0,
            "max_quote_per_trade": 100.0,
            "max_daily_quote": 500.0,
            "max_active_proposals": 1,
            "min_stop_distance_pct": 0.5,
            "max_stop_distance_pct": 3.0,
            "atr_stop_multiplier": 1.5,
            "min_reward_risk": 2.0,
            "proposal_ttl_minutes": 15,
            "execution_lease_seconds": 300,
            "max_live_arm_minutes": 1440,
            "paper_fee_pct": 0.1,
        },
        "openclaw": {
            "command": "/bin/false",
            "telegram_owner_id": "123456789",
        },
        "codex": {
            "command": "/bin/false",
            "mcp_server": "binance-marketdata",
            "endpoint": "https://agent.binance.com/mcp/agentic",
            "timeout_seconds": 30,
            "model": None,
            "read_only": True,
            "close_grace_seconds": 2,
            "agent_os_home": str(profile),
            "agent_os_workspace": str(profile / "workspace"),
        },
        "security": {
            "require_manual_approval": True,
            "allowed_product": "spot",
            "allow_withdrawal": False,
            "allow_futures": False,
            "allow_margin": False,
            "allow_transfer": False,
        },
        "paper": {"initial_balance_usdt": 1000, "max_quote_per_entry_usdt": 100, "max_active_tranches": 10, "max_economic_positions": 5, "max_open_exposure_usdt": 500, "max_successful_entries_per_utc_day": 10, "max_risk_per_position_usdt": 2, "max_aggregate_risk_usdt": 4, "daily_realized_loss_cap_usdt": 5, "close_proposal_ttl_seconds": 300, "slippage_pct": 0.05},
        "live": {"enabled": False, "armed": False, "max_quote_per_entry_usdt": 100, "max_active_tranches": 10, "max_economic_positions": 5, "max_open_exposure_usdt": 500, "min_free_reserve_usdt": 8, "max_risk_per_position_usdt": 2, "max_aggregate_risk_usdt": 4, "max_successful_entries_per_utc_day": 10, "max_pending_proposals": 1, "approval_ttl_seconds": 60, "daily_realized_loss_cap_usdt": 5, "weekly_loss_cap_usdt": 20, "protective_orders_available": False},
    }


def write_config(root: Path, mode: str = "paper") -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(config_dict(root, mode=mode), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
