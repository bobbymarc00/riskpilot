from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from spotguard.config import load_settings
from spotguard.policy import PolicyError, build_proposal
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow

from tests.helpers import write_config


class PolicyTests(unittest.TestCase):
    def _candidate(self, root: Path):
        settings = load_settings(write_config(root))
        service = SpotGuard(settings)
        scan = service.scan(symbols=["BTCUSDT"], synthetic=True)
        candidate = scan["results"][0]["candidate"]
        self.assertIsNotNone(candidate)
        return settings, candidate

    def test_builds_policy_owned_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            proposal = build_proposal(
                settings,
                candidate,
                Decimal(str(candidate["price"])) * Decimal("0.999"),
                Decimal(str(candidate["price"])),
                Decimal("6"),
                "Trend and spread passed the paper review.",
            )
            self.assertEqual(proposal["product"], "SPOT")
            self.assertEqual(proposal["side"], "BUY")
            self.assertEqual(proposal["quote_amount"], "6")
            self.assertGreaterEqual(Decimal(proposal["reward_risk"]), Decimal("2"))

    def test_rejects_oversized_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            with self.assertRaisesRegex(PolicyError, "quote amount"):
                build_proposal(
                    settings,
                    candidate,
                    Decimal(str(candidate["price"])) * Decimal("0.999"),
                    Decimal(str(candidate["price"])),
                    Decimal("101"),
                    "Oversized request.",
                )

    def test_rejects_stale_price_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            stale = Decimal(str(candidate["price"])) * Decimal("1.02")
            with self.assertRaisesRegex(PolicyError, "drifted"):
                build_proposal(
                    settings,
                    candidate,
                    stale * Decimal("0.999"),
                    stale,
                    Decimal("6"),
                    "Stale price.",
                )

    def test_rejects_expired_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            candidate["expires_at"] = isoformat(utcnow() - timedelta(seconds=1))
            with self.assertRaisesRegex(PolicyError, "expired"):
                build_proposal(
                    settings,
                    candidate,
                    Decimal(str(candidate["price"])) * Decimal("0.999"),
                    Decimal(str(candidate["price"])),
                    Decimal("6"),
                    "Expired candidate.",
                )

    def test_rejects_wide_order_book_spread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings, candidate = self._candidate(Path(directory))
            ask = Decimal(str(candidate["price"]))
            bid = ask * Decimal("0.99")
            with self.assertRaisesRegex(PolicyError, "spread"):
                build_proposal(settings, candidate, bid, ask, Decimal("6"), "Wide spread.")


if __name__ == "__main__":
    unittest.main()
