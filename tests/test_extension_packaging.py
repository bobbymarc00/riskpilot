from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extensions/riskpilot-direct-review"


class ExtensionPackagingTests(unittest.TestCase):
    def test_canonical_extension_contains_only_runtime_source_and_manifests(self) -> None:
        self.assertEqual(
            sorted(path.name for path in EXTENSION.iterdir() if path.is_file()),
            ["index.js", "openclaw.plugin.json", "package.json"],
        )
        manifest = json.loads((EXTENSION / "openclaw.plugin.json").read_text(encoding="utf-8"))
        package = json.loads((EXTENSION / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "riskpilot-direct-review")
        self.assertTrue(manifest["activation"]["onStartup"])
        self.assertEqual(package["openclaw"]["extensions"], ["./index.js"])

    def test_installer_copies_canonical_extension_without_global_configuration(self) -> None:
        installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        self.assertIn('spotguard_extension_source="$spotguard_project_dir/extensions/riskpilot-direct-review"', installer)
        self.assertIn('install -m 0644 "$spotguard_extension_source/index.js"', installer)
        self.assertIn('install -m 0644 "$spotguard_extension_source/openclaw.plugin.json"', installer)
        self.assertIn('install -m 0644 "$spotguard_extension_source/package.json"', installer)
        self.assertNotIn("openclaw.json", installer)
