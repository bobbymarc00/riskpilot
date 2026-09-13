from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from spotguard.config import load_settings
from spotguard.live_execution import LiveExecutionAdapter
from spotguard.service import SpotGuard
from spotguard.util import isoformat

from .helpers import config_dict


class LivePermissionDiagnosticsTests(TestCase):
    def _adapter_and_proof(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        raw = config_dict(root)
        raw["live"]["enabled"] = True
        path = root / "config.json"
        path.write_text(__import__("json").dumps(raw), encoding="utf-8")
        adapter = LiveExecutionAdapter(load_settings(path))
        schema = "schema-fingerprint"
        verified_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        proof = {
            "result": "verified", "delegated_operation": "spot.orderTest",
            "profile_fingerprint": adapter.execution_profile_fingerprint(),
            "schema_fingerprint": schema, "verified_at": isoformat(verified_at),
            "expires_at": isoformat(verified_at + timedelta(seconds=adapter._PERMISSION_ATTESTATION_TTL_SECONDS)),
        }
        return adapter, schema, verified_at, proof

    def test_permission_proof_transitions_verified_stale_verified_without_fail_open(self):
        adapter, schema, verified_at, proof = self._adapter_and_proof()
        with patch("spotguard.live_execution.utcnow", return_value=verified_at + timedelta(seconds=30)):
            fresh = adapter._permission_diagnostics(proof, schema, account_type="SPOT", can_trade=True,
                                                     permission_metadata=None, attestation_valid=True)
        with patch("spotguard.live_execution.utcnow", return_value=verified_at + timedelta(seconds=3601)):
            stale = adapter._permission_diagnostics(proof, schema, account_type="SPOT", can_trade=True,
                                                     permission_metadata=None, attestation_valid=False)
        refreshed = dict(proof)
        refreshed["verified_at"] = isoformat(verified_at + timedelta(seconds=3602))
        refreshed["expires_at"] = isoformat(verified_at + timedelta(seconds=7202))
        with patch("spotguard.live_execution.utcnow", return_value=verified_at + timedelta(seconds=3603)):
            revalidated = adapter._permission_diagnostics(refreshed, schema, account_type="SPOT", can_trade=True,
                                                           permission_metadata=None, attestation_valid=True)
        self.assertEqual((fresh["status"], stale["status"], revalidated["status"]),
                         ("verified", "stale", "verified"))
        self.assertFalse(stale["verified"])

    def test_timeout_is_unknown_and_explicit_binance_negative_is_denied(self):
        adapter, schema, _, proof = self._adapter_and_proof()
        unknown = adapter._permission_diagnostics(proof, schema, account_type=None, can_trade=None,
                                                  permission_metadata={"verified": False, "reasons": ["api_restrictions_read: timeout"]},
                                                  attestation_valid=False)
        denied = adapter._permission_diagnostics(proof, schema, account_type="SPOT", can_trade=True,
                                                 permission_metadata={"verified": False, "tool": "apiRestrictions",
                                                                      "reasons": ["api_restrictions_spot_trade_not_enabled"]},
                                                 attestation_valid=False)
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(denied["status"], "denied")

    def test_expired_persisted_proof_cannot_make_telegram_ready_in_same_or_new_process(self):
        adapter, schema, verified_at, proof = self._adapter_and_proof()
        service = SpotGuard(adapter.settings)
        service.ledger.grant_live_authorization("la-test", isoformat(verified_at),
                                                isoformat(verified_at + timedelta(days=7)),
                                                adapter.settings.openclaw.telegram_owner_id,
                                                adapter.settings.telegram.chat_id)
        expired = dict(proof); expired["expires_at"] = isoformat(verified_at - timedelta(seconds=1))
        decimal = {**expired, "wire_mode": "fixed-point-json-number",
                   "classification": "REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG",
                   "scope": "bounded_decimal_domain", "tested_fields": ["quantity", "price"],
                   "minimum_verified_fractional_number": "0.001"}
        service.ledger.add_event("live.trade_permission_attestation", None, expired)
        service.ledger.add_event("live.decimal_transport_attestation", None, decimal)
        healthy = {name: True for name in (
            "binance_mcp_connected", "execution_profile_configured", "codex_login_reported",
            "account_read_verified", "open_orders_read_verified", "spot_trade_scope_verified",
            "write_tool_discovered", "write_schema_verified", "decimal_transport_verified",
            "protective_order_capability_verified", "symbol_exchange_flags_verified",
            "live_limits_valid", "live_enabled", "live_armed")}
        healthy.update({"execution_ready": True, "blockers": [], "test_order_schema_fingerprint": schema,
                        "spot_trade_permission": {"status": "unknown", "verified": False}})
        service.live_arm.status = lambda: type("Arm", (), {"armed": True, "scope": "FULL"})()
        service.live_status = lambda **_: dict(healthy)
        at_expiry = verified_at + timedelta(seconds=3601)
        with patch("spotguard.service.utcnow", return_value=at_expiry), \
             patch("spotguard.live_execution.utcnow", return_value=at_expiry):
            first = service.telegram_live_readiness(adapter.settings.openclaw.telegram_owner_id, adapter.settings.telegram.chat_id)
            second = service.telegram_live_readiness(adapter.settings.openclaw.telegram_owner_id, adapter.settings.telegram.chat_id)
            restarted = SpotGuard(adapter.settings)
            restarted.live_arm.status = service.live_arm.status
            restarted.live_status = service.live_status
            third = restarted.telegram_live_readiness(adapter.settings.openclaw.telegram_owner_id, adapter.settings.telegram.chat_id)
        self.assertFalse(first["execution_ready"])
        self.assertFalse(second["execution_ready"])
        self.assertFalse(third["execution_ready"])
