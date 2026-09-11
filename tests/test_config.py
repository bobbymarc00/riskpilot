from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from decimal import Decimal

from spotguard.config import ConfigError, load_settings

from tests.helpers import config_dict, write_config


class ConfigTests(unittest.TestCase):
    def test_safe_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_settings(write_config(root))
            self.assertEqual(settings.mode, "paper")
            self.assertEqual(settings.security.allowed_product, "spot")
            self.assertFalse(settings.security.allow_withdrawal)
            self.assertTrue(settings.codex.read_only)
            self.assertEqual(settings.codex.mcp_server, "binance-marketdata")

    def test_paper_and_live_limits_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["paper"]["max_active_tranches"] = 10
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_settings(path, create_state=False)
            self.assertEqual(settings.paper.max_open_positions, 10)
            self.assertEqual(settings.live.max_economic_positions, 5)
            self.assertEqual(settings.live.max_active_tranches, 10)
            raw["paper"]["max_active_tranches"] = 9
            path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(load_settings(path, create_state=False).paper.max_active_tranches, 9)

    def test_legacy_field_mapping_preserves_numeric_ceilings_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["risk"].update({"default_quote_amount": 10.0, "max_quote_per_trade": 20.0})
            raw["paper"] = {"initial_balance_usdt": 28, "max_open_positions": 1, "slippage_pct": 0.05, "max_risk_per_trade_usdt": 1}
            raw["live"] = {"enabled": False, "arm": False, "max_live_trade_usdt": 10,
                "allowed_symbols": raw["market"]["symbols"], "max_open_positions": 1,
                "approval_ttl_seconds": 60, "daily_loss_cap_usdt": 10,
                "weekly_loss_cap_usdt": 20, "protective_orders_available": False}
            path = root / "config.json"; path.write_text(json.dumps(raw))
            settings = load_settings(path, create_state=False)
            self.assertEqual(settings.risk.max_quote_per_trade, Decimal("20.0"))
            self.assertEqual(settings.paper.max_risk_per_position_usdt, Decimal("1"))
            self.assertEqual(settings.live.max_quote_per_entry_usdt, Decimal("10"))
            self.assertEqual(settings.paper.max_open_positions, 1)
            self.assertEqual(settings.live.min_free_reserve_usdt, Decimal("8"))
            self.assertEqual(json.loads(path.read_text())["risk"]["max_quote_per_trade"], 20.0)

    def test_live_entry_slippage_cap_is_hard_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            path = root / "config.json"
            for invalid in ("-0.01", "1.01"):
                raw["live"]["entry_slippage_cap_pct"] = invalid
                path.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "live limits are invalid"):
                    load_settings(path, create_state=False)

    def test_embedded_exchange_secret_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["binance_api_key"] = "must-never-be-here"
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "credentials belong in Codex/Agent OS OAuth"):
                load_settings(path, create_state=False)

    def test_dangerous_product_flags_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["security"]["allow_futures"] = True
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must remain false"):
                load_settings(path, create_state=False)

    def test_manual_approval_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["security"]["require_manual_approval"] = False
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must remain true"):
                load_settings(path, create_state=False)

    def test_live_execution_is_locked_in_codex_bridge_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ConfigError, "live mode"):
                load_settings(write_config(root, mode="live"), create_state=False)

    def test_codex_bridge_cannot_be_made_write_capable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["codex"]["read_only"] = False
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must remain true"):
                load_settings(path, create_state=False)

    def test_explicit_legacy_oauth_profile_requires_neutral_paper_only_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_home = root / "home" / ".codex"
            old_home.mkdir(parents=True)
            (old_home / "config.toml").write_text("# old OAuth profile\n")
            neutral = root / "neutral-workspace"; neutral.mkdir()
            raw = config_dict(root)
            raw["codex"].update({"agent_os_home": str(old_home), "agent_os_workspace": str(neutral), "legacy_oauth_profile": True})
            path = root / "config.json"; path.write_text(json.dumps(raw))
            with patch("spotguard.config.Path.home", return_value=root / "home"):
                settings = load_settings(path, create_state=False)
            self.assertTrue(settings.codex.legacy_oauth_profile)
            raw["scheduled_proposal_mode"] = "live"; path.write_text(json.dumps(raw))
            with patch("spotguard.config.Path.home", return_value=root / "home"):
                with self.assertRaisesRegex(ConfigError, "PAPER-only"):
                    load_settings(path, create_state=False)

    def test_v010_config_without_codex_section_upgrades_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            del raw["codex"]
            raw["openclaw"]["mcp_server"] = "binance-agent-os"
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_settings(path, create_state=False)
            self.assertEqual(settings.codex.command, "codex")
            self.assertTrue(settings.codex.read_only)


if __name__ == "__main__":
    unittest.main()
