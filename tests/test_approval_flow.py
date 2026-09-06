from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from spotguard.config import load_settings
from spotguard.db import LedgerError
from spotguard.policy import PolicyError
from spotguard.security import SecurityError
from spotguard.service import SpotGuard

from tests.helpers import config_dict, write_config


class ApprovalFlowTests(unittest.TestCase):
    def _proposal(self, root: Path, mode: str = "paper"):
        settings = load_settings(write_config(root, mode=mode))
        service = SpotGuard(settings)
        scan = service.scan(symbols=["BTCUSDT"], synthetic=True)
        candidate = scan["results"][0]["candidate"]
        result = service.create_proposal(
            candidate["id"],
            Decimal(str(candidate["price"])) * Decimal("0.999"),
            Decimal(str(candidate["price"])),
            Decimal("6"),
            "Candidate remains valid after a fresh read-only review.",
        )
        proposal = result["proposal"]
        token = service.signer.approval_token(proposal["canonical_json"])
        return settings, service, proposal, token

    def test_wrong_sender_cannot_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, service, proposal, token = self._proposal(Path(directory))
            with self.assertRaisesRegex(SecurityError, "not the configured Telegram owner"):
                service.claim(proposal["id"], token, "9999999999")

    def test_double_click_cannot_execute_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, token = self._proposal(Path(directory))
            claim = service.claim(proposal["id"], token, settings.openclaw.telegram_owner_id)
            with self.assertRaisesRegex((LedgerError, PolicyError, SecurityError), "not claimable|PENDING|active execution"):
                service.claim(proposal["id"], token, settings.openclaw.telegram_owner_id)
            completed = service.execute_paper(proposal["id"], claim["lease"])
            self.assertEqual(completed["status"], "EXECUTED")
            self.assertTrue(completed["execution_order_id"].startswith("paper-"))
            with self.assertRaisesRegex(SecurityError, "active execution"):
                service.execute_paper(proposal["id"], claim["lease"])

    def test_daily_cap_is_rechecked_inside_atomic_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["risk"]["max_active_proposals"] = 3
            config = root / "config.json"
            config.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_settings(config)
            service = SpotGuard(settings)
            proposals = []
            for symbol in settings.market.symbols:
                candidate = service.scan(symbols=[symbol], synthetic=True)["results"][0]["candidate"]
                proposal = service.create_proposal(
                    candidate["id"],
                    Decimal(str(candidate["price"])) * Decimal("0.999"),
                    Decimal(str(candidate["price"])),
                    Decimal("6"),
                    f"Paper review passed for {symbol}.",
                )["proposal"]
                token = service.signer.approval_token(proposal["canonical_json"])
                proposals.append((proposal, token))
            for proposal, token in proposals[:2]:
                service.claim(proposal["id"], token, settings.openclaw.telegram_owner_id)
            third, third_token = proposals[2]
            service.ledger.successful_paper_entries = lambda _day: 10  # type: ignore[method-assign]
            with self.assertRaisesRegex(PolicyError, "daily PAPER entry quota reached"):
                service.claim(third["id"], third_token, settings.openclaw.telegram_owner_id)
            self.assertEqual(service.ledger.get_proposal(third["id"])["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
