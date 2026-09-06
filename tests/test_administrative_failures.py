from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import _local_admin_update, _risk_profile_changes, main
from spotguard.config import load_settings
from spotguard.service import SpotGuard
from tests.helpers import config_dict


OWNER = "123456789"


class AdministrativeFailureTests(unittest.TestCase):
    def _service(self, root: Path) -> tuple[Path, SpotGuard]:
        config = root / "config.json"
        config.write_text(json.dumps(config_dict(root)), encoding="utf-8")
        config.chmod(0o600)
        return config, SpotGuard(load_settings(config))

    def test_invalid_config_and_wrong_owner_phrase_are_non_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config, _ = self._service(root)
            before = config.read_bytes()
            raw = config_dict(root); raw["paper"]["max_quote_per_entry_usdt"] = "not-a-number"
            config.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(Exception):
                load_settings(config)
            config.write_bytes(before)
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="WRONG"):
                self.assertEqual(main(["--config", str(config), "--json", "scheduled-mode", "set", "live", "--owner-id", OWNER]), 2)
            self.assertEqual(config.read_bytes(), before)

    def test_failed_audit_restores_atomic_update(self):
        with tempfile.TemporaryDirectory() as directory:
            config, service = self._service(Path(directory)); before = config.read_bytes()
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="SET SCHEDULED PROPOSAL MODE LIVE"), patch.object(
                    service.ledger, "add_event", side_effect=[None, RuntimeError("audit unavailable")]):
                with self.assertRaisesRegex(Exception, "audit failed"):
                    _local_admin_update(service, OWNER, "SET SCHEDULED PROPOSAL MODE LIVE", {"scheduled_proposal_mode": "live"})
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(config.with_name("config.json.tmp").exists())
            self.assertFalse(config.with_name("config.json.rollback").exists())

    def test_interrupted_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            config, service = self._service(Path(directory)); before = config.read_bytes()
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="SET SCHEDULED PROPOSAL MODE LIVE"), patch.object(
                    Path, "replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _local_admin_update(service, OWNER, "SET SCHEDULED PROPOSAL MODE LIVE", {"scheduled_proposal_mode": "live"})
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse(config.with_name("config.json.tmp").exists())
            self.assertFalse(config.with_name("config.json.rollback").exists())

    def test_profile_update_is_validated_audited_and_keeps_timestamped_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config, service = self._service(root)
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="APPLY RISKPILOT PAPER AND LIVE RISK PROFILE"):
                result = _local_admin_update(service, OWNER, "APPLY RISKPILOT PAPER AND LIVE RISK PROFILE", _risk_profile_changes())
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(Path(result["backup"]).read_bytes(), json.dumps(config_dict(root)).encode("utf-8"))
            settings = load_settings(config)
            self.assertEqual(str(settings.paper.max_quote_per_entry_usdt), "100")
            self.assertEqual(str(settings.live.max_quote_per_entry_usdt), "100")
            self.assertEqual(settings.live.max_economic_positions, 5)
            self.assertEqual(settings.live.max_active_tranches, 10)
            self.assertFalse(settings.live.enabled)
            self.assertFalse(settings.execution_ready)


if __name__ == "__main__":
    unittest.main()
