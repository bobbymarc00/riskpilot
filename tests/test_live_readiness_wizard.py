from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from spotguard.config import load_settings
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.util import isoformat, parse_time, utcnow

from .helpers import config_dict


class LiveReadinessWizardTests(TestCase):
    def make_service(self) -> SpotGuard:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        raw = config_dict(root)
        raw["live"]["enabled"] = True
        path = root / "config.json"
        path.write_text(__import__("json").dumps(raw), encoding="utf-8")
        return SpotGuard(load_settings(path))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_readiness_is_read_only_and_activation_challenge_is_bound(self):
        service = self.make_service()
        service.live_status = Mock(return_value={"execution_ready": False})
        before = service.ledger.active_live_authorization()
        result = service.telegram_live_readiness("123456789", "123456789")
        self.assertFalse(result["execution_ready"])
        self.assertIsNone(before); self.assertIsNone(service.ledger.active_live_authorization())
        service.request_live_activation("123456789", "123456789")
        with self.assertRaises(SecurityError):
            service.confirm_live_activation("READY TO LIVE TRADE", "other", "123456789")
        with self.assertRaises(SecurityError):
            service.confirm_live_activation("not exact", "123456789", "123456789")

    def test_confirmation_is_one_time_and_deactivation_revokes(self):
        service = self.make_service()
        service.request_live_deactivation("123456789", "123456789")
        with self.assertRaises(SecurityError):
            service.confirm_live_deactivation("DISABLE LIVE TRADE", "123456789", "123456789")
        service.request_live_deactivation("123456789", "123456789")
        result = service.confirm_live_deactivation("DISABLE LIVE TRADING", "123456789", "123456789")
        self.assertTrue(result["ok"])
        with self.assertRaises(SecurityError):
            service.confirm_live_deactivation("DISABLE LIVE TRADING", "123456789", "123456789")

    def test_lease_persists_and_expires_hard(self):
        service = self.make_service()
        now = utcnow()
        service.ledger.grant_live_authorization("la-test", isoformat(now), isoformat(now + timedelta(days=7)), "123456789", "123456789")
        recreated = SpotGuard(service.settings)
        self.assertEqual(recreated.ledger.active_live_authorization()["authorization_id"], "la-test")
        recreated.ledger.grant_live_authorization("la-expired", isoformat(now - timedelta(days=8)), isoformat(now - timedelta(seconds=1)), "123456789", "123456789")
        self.assertIsNone(recreated._authorization_record())
        self.assertEqual(recreated.ledger.active_live_authorization(), None)

    def test_status_distinguishes_revoked_from_expired(self):
        service = self.make_service()
        now = utcnow()
        service.live_status = Mock(return_value=self._healthy_readiness())
        service.ledger.grant_live_authorization(
            "la-revoked", isoformat(now), isoformat(now + timedelta(days=7)),
            "123456789", "123456789",
        )
        service.ledger.revoke_live_authorization("USER_REQUEST", isoformat(now))
        revoked = service.telegram_live_readiness("123456789", "123456789")
        self.assertIn("Authorization: REVOKED", revoked["presentation"]["text"])
        self.assertFalse(revoked["execution_ready"])
        lease = service.ledger.latest_live_authorization()
        self.assertEqual(lease["status"], "REVOKED")
        self.assertIsNotNone(lease["revoked_at"])
        self.assertEqual(lease["revoke_reason"], "USER_REQUEST")

        service.ledger.grant_live_authorization(
            "la-expired-display", isoformat(now + timedelta(seconds=1)),
            isoformat(now - timedelta(seconds=1)), "123456789", "123456789",
        )
        expired = service.telegram_live_readiness("123456789", "123456789")
        self.assertIn("Authorization: EXPIRED", expired["presentation"]["text"])

    def test_failed_preflight_never_grants_lease(self):
        service = self.make_service()
        service.request_live_activation("123456789", "123456789")
        service.prepare_live_session = Mock(return_value={"blockers": ["SPOT_TRADE_PERMISSION_UNAVAILABLE"]})
        result = service.confirm_live_activation("READY TO LIVE TRADE", "123456789", "123456789")
        self.assertFalse(result["ok"])
        self.assertIsNone(service.ledger.active_live_authorization())

    def test_successful_confirmation_grants_exact_seven_day_lease_without_order(self):
        service = self.make_service()
        service.request_live_activation("123456789", "123456789")
        service.prepare_live_session = Mock(return_value={"blockers": ["live_armed"]})
        service.live_status = Mock(return_value={"execution_ready": True})
        result = service.confirm_live_activation("READY TO LIVE TRADE", "123456789", "123456789")
        self.assertTrue(result["ok"])
        lease = result["authorization"]
        self.assertEqual((parse_time(lease["expires_at"]) - parse_time(lease["activated_at"])).total_seconds(), 7 * 24 * 60 * 60)
        self.assertEqual(service.ledger.list_proposals(), [])

    @staticmethod
    def _healthy_readiness(*, connected: bool = True) -> dict:
        return {
            "binance_mcp_connected": connected, "execution_profile_configured": True,
            "codex_login_reported": connected, "account_read_verified": True,
            "open_orders_read_verified": True, "spot_trade_scope_verified": True,
            "write_tool_discovered": True, "write_schema_verified": True,
            "decimal_transport_verified": True, "protective_order_capability_verified": True,
            "symbol_exchange_flags_verified": True, "live_limits_valid": True,
            "live_enabled": True, "live_armed": True,
            "execution_ready": connected, "blockers": [] if connected else ["binance_mcp_connected"],
        }

    def test_telegram_and_entry_guard_share_connection_critical_decision(self):
        service = self.make_service(); now = utcnow()
        service.ledger.grant_live_authorization("la-active", isoformat(now), isoformat(now + timedelta(days=7)), "123456789", "123456789")
        service.live_arm.arm(60)
        # This test isolates the shared connection decision; proof persistence
        # is exercised separately by the readiness invariant tests.
        service._live_readiness_invariant = Mock(return_value=True)
        service.live_status = Mock(return_value=self._healthy_readiness(connected=False))
        telegram = service.telegram_live_readiness("123456789", "123456789")
        self.assertFalse(telegram["execution_ready"])
        self.assertFalse(service.is_live_entry_authorized())
        self.assertEqual(service.ledger.active_live_authorization()["status"], "ACTIVE")
        expires_at = service.ledger.active_live_authorization()["expires_at"]
        service.live_status = Mock(return_value=self._healthy_readiness(connected=True))
        self.assertTrue(service.telegram_live_readiness("123456789", "123456789")["execution_ready"])
        self.assertTrue(service.is_live_entry_authorized())
        self.assertEqual(service.ledger.active_live_authorization()["expires_at"], expires_at)
