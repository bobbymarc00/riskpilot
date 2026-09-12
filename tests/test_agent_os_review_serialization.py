from __future__ import annotations

import json
import unittest

from spotguard.market import Kline
from spotguard.service import SpotGuard
from spotguard.util import json_default


class AgentOSReviewSerializationTests(unittest.TestCase):
    def test_known_klines_are_json_safe_at_any_review_nesting_level(self) -> None:
        candle = Kline(1, 1.1, 1.2, 1.0, 1.15, 42.0, 2)
        review = SpotGuard._serialize_agent_os_review({
            "candle": candle, "candles": [candle], "evidence": {"nested": [candle]},
        })
        payload = json.loads(json.dumps(review, default=json_default))
        self.assertEqual(payload["candle"], {
            "open_time": 1, "open": 1.1, "high": 1.2, "low": 1.0,
            "close": 1.15, "volume": 42.0, "close_time": 2,
        })
        self.assertEqual(payload["evidence"]["nested"][0]["close_time"], 2)

    def test_unknown_review_object_still_fails_loudly(self) -> None:
        class Unsupported:
            pass
        review = SpotGuard._serialize_agent_os_review({"unknown": Unsupported()})
        with self.assertRaises(TypeError):
            json.dumps(review, default=json_default)
