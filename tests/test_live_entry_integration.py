from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock, patch

from spotguard.market import SpotMarketSnapshot, scaled_synthetic_klines
from spotguard.service import SpotGuard
from tests.test_live_authorization_lifecycle import LiveAuthorizationLifecycleTests


class LiveEntryIntegrationTests(LiveAuthorizationLifecycleTests):
    """Direct entry exercises the real proposal builder, never an order write."""
    def _entry_fixture(self):
        auth = self._grant(); self._proofs(auth); self._status()
        self.now = self.t0.replace(hour=23, minute=53)
        def refresh(*_, **__):
            self._proofs(auth, decimal_at=self.now); return {"execution_ready": False}
        self.service.prepare_live_session = Mock(side_effect=refresh)
        self.service.live_executor.execute = Mock()
        self.service._validate_live_entry_limits = Mock(return_value={})
        return auth

    @patch("spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(100))
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SpotMarketSnapshot("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("5"),Decimal("0.001"),"TRADING",1))
    def test_direct_live_buy_refreshes_stale_decimal_and_stays_pending(self, _market, _klines):
        auth=self._entry_fixture(); old=self.service.ledger.latest_event("live.decimal_transport_attestation")["event_id"]
        with patch("spotguard.service.utcnow",return_value=self.now),patch("spotguard.live_execution.utcnow",return_value=self.now):
            result=self.service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
        proposal=result["proposal"]; proof=self.service.ledger.latest_event("live.decimal_transport_attestation")
        self.assertEqual(proposal["mode"],"live"); self.assertEqual(proposal["status"],"PENDING")
        self.assertGreater(proof["event_id"],old); self.assertEqual(auth["authorization_id"],self.service.ledger.active_live_authorization()["authorization_id"])
        self.service.live_executor.execute.assert_not_called()

    @patch("spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(100))
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SpotMarketSnapshot("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("5"),Decimal("0.001"),"TRADING",1))
    def test_direct_live_buy_refresh_failure_then_retry(self, _market, _klines):
        self._entry_fixture(); self.service.prepare_live_session=Mock(side_effect=TimeoutError("timeout"))
        with patch("spotguard.service.utcnow",return_value=self.now),patch("spotguard.live_execution.utcnow",return_value=self.now):
            with self.assertRaises(Exception): self.service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
        self.service.live_executor.execute.assert_not_called()
        auth=self.service.ledger.active_live_authorization()
        self.service.prepare_live_session=Mock(side_effect=lambda *_,**__: (self._proofs(auth,decimal_at=self.now) or {"execution_ready":False}))
        with patch("spotguard.service.utcnow",return_value=self.now),patch("spotguard.live_execution.utcnow",return_value=self.now):
            result=self.service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
        self.assertEqual(result["proposal"]["status"],"PENDING"); self.service.live_executor.execute.assert_not_called()
