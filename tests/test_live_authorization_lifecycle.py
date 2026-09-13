from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from spotguard.config import load_settings
from spotguard.service import SpotGuard
from spotguard.util import isoformat
from .helpers import config_dict


class LiveAuthorizationLifecycleTests(TestCase):
    """No network: exercise the production entry evaluator over persisted events."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); raw = config_dict(root); raw["live"]["enabled"] = True
        path = root / "config.json"; path.write_text(json.dumps(raw), encoding="utf8")
        self.service = SpotGuard(load_settings(path)); self.t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.owner = self.service.settings.openclaw.telegram_owner_id; self.chat = self.service.settings.telegram.chat_id
        self.schema = "schema-A"; self.profile = self.service.live_executor.execution_profile_fingerprint()
        self.service.live_arm.status = Mock(return_value=type("Arm", (), {"armed": True, "scope": "FULL"})())

    def _grant(self, name="la-A"):
        return self.service.ledger.grant_live_authorization(name, isoformat(self.t0), isoformat(self.t0 + timedelta(days=7)), self.owner, self.chat)

    def _proofs(self, auth, *, decimal_at=None, schema=None):
        schema = schema or self.schema; decimal_at = decimal_at or self.t0
        permission = {"result":"verified", "authorization_id":auth["authorization_id"], "verified_at":isoformat(self.t0),
                      "expires_at":auth["expires_at"], "delegated_operation":"spot.orderTest", "profile_fingerprint":self.profile,
                      "schema_fingerprint":schema}
        decimal = {"result":"verified", "verified_at":isoformat(decimal_at),
                   "expires_at":isoformat(decimal_at + timedelta(seconds=3600)), "delegated_operation":"spot.orderTest",
                   "profile_fingerprint":self.profile, "schema_fingerprint":schema, "wire_mode":"fixed-point-json-number",
                   "classification":"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG", "scope":"bounded_decimal_domain",
                   "tested_fields":["quantity","price"], "minimum_verified_fractional_number":"0.001"}
        self.service.ledger.add_event("live.trade_permission_attestation", None, permission)
        self.service.ledger.add_event("live.decimal_transport_attestation", None, decimal)
        return permission, decimal

    def _status(self, *, schema=None):
        schema = schema or self.schema
        ok = {key: True for key in ("binance_mcp_connected","execution_profile_configured","codex_login_reported",
              "account_read_verified","open_orders_read_verified","spot_trade_scope_verified","write_tool_discovered",
              "write_schema_verified","decimal_transport_verified","protective_order_capability_verified",
              "symbol_exchange_flags_verified","live_limits_valid","live_enabled","live_armed")}
        ok.update({"execution_ready":True,"blockers":[],"test_order_schema_fingerprint":schema,
                   "spot_trade_permission":{"status":"verified","verified":True}})
        self.service.live_status = Mock(return_value=ok)

    def _at(self, now):
        return patch("spotguard.service.utcnow", return_value=now), patch("spotguard.live_execution.utcnow", return_value=now)

    def test_seven_day_matrix_refreshes_decimal_but_not_permission(self):
        auth=self._grant(); permission, old_decimal=self._proofs(auth); self._status()
        def refresh(*_, **__):
            self._proofs(auth, decimal_at=self.now); return {"execution_ready":False}
        self.service.prepare_live_session=Mock(side_effect=refresh)
        for offset, expected in ((timedelta(),True),(timedelta(minutes=59),True),(timedelta(hours=1,seconds=1),True),
                                 (timedelta(hours=2),True),(timedelta(days=1),True),(timedelta(days=6),True),
                                 (timedelta(days=6,hours=23,minutes=59),True),(timedelta(days=7,seconds=1),False)):
            self.now=self.t0+offset; a,b=self._at(self.now)
            with a,b: result=self.service.telegram_live_readiness(self.owner,self.chat)
            self.assertEqual(result["execution_ready"], expected, str(offset))
        self.assertEqual(permission["expires_at"], auth["expires_at"])
        self.assertGreater(self.service.ledger.latest_event("live.decimal_transport_attestation")["event_id"], old_decimal.get("event_id",0))

    def test_decimal_timeout_then_recovery_and_schema_change(self):
        auth=self._grant(); _, old=self._proofs(auth); self._status(); now=self.t0+timedelta(hours=1,seconds=1)
        self.service.prepare_live_session=Mock(side_effect=TimeoutError("MCP timeout"))
        with self._at(now)[0], self._at(now)[1]: self.assertFalse(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
        old_id=self.service.ledger.latest_event("live.decimal_transport_attestation")["event_id"]
        def success(*_,**__): self._proofs(auth,decimal_at=now,schema="schema-B"); return {"execution_ready":False}
        self._status(schema="schema-B"); self.service.prepare_live_session=Mock(side_effect=success)
        with self._at(now)[0], self._at(now)[1]: self.assertTrue(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
        proof=self.service.ledger.latest_event("live.decimal_transport_attestation")
        self.assertGreater(proof["event_id"],old_id); self.assertEqual(proof["schema_fingerprint"],"schema-B")

    def test_revoke_and_new_authorization_never_reuses_old_permission(self):
        auth_a=self._grant(); self._proofs(auth_a); self._status()
        with self._at(self.t0)[0],self._at(self.t0)[1]: self.assertTrue(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
        self.service.ledger.revoke_live_authorization("TEST",isoformat(self.t0+timedelta(hours=2)))
        with self._at(self.t0+timedelta(hours=2))[0],self._at(self.t0+timedelta(hours=2))[1]: self.assertFalse(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
        auth_b=self.service.ledger.grant_live_authorization("la-B",isoformat(self.t0+timedelta(hours=2)),isoformat(self.t0+timedelta(days=7)),self.owner,self.chat)
        self.assertNotEqual(auth_a["authorization_id"],auth_b["authorization_id"])
        with self._at(self.t0+timedelta(hours=2))[0],self._at(self.t0+timedelta(hours=2))[1]: self.assertFalse(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
        self._proofs(auth_b,decimal_at=self.t0+timedelta(hours=2))
        with self._at(self.t0+timedelta(hours=2))[0],self._at(self.t0+timedelta(hours=2))[1]: self.assertTrue(self.service.telegram_live_readiness(self.owner,self.chat)["execution_ready"])
