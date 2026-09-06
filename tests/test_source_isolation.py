from __future__ import annotations

import unittest
from pathlib import Path


class SourceIsolationTests(unittest.TestCase):
    def test_runtime_has_no_technocore_identity_or_coreflux_reference(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "spotguard"
        combined = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py")).lower()
        self.assertNotIn("identity.pem", combined)
        self.assertNotIn(".technocore", combined)
        self.assertNotIn("coreflux", combined)


if __name__ == "__main__":
    unittest.main()
