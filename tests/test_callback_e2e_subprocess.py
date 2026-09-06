from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from spotguard.config import load_settings
from spotguard.service import SpotGuard
from tests.helpers import config_dict


ROOT = Path(__file__).parents[1]
OWNER = "123456789"


class CallbackFreshSubprocessTests(unittest.TestCase):
    def _config(self, root: Path, *, mode: str = "paper") -> tuple[Path, Path]:
        capture = root / "presentation.json"
        openclaw = root / "fake-openclaw.py"
        openclaw.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "args=sys.argv[1:]\n"
            "if args[-1:] == ['--help']:\n"
            " print('--presentation')\n"
            " raise SystemExit(0)\n"
            "if '--presentation' in args:\n"
            f" pathlib.Path({str(capture)!r}).write_text(args[args.index('--presentation')+1], encoding='utf-8')\n"
            "print(json.dumps({'sent': True}))\n",
            encoding="utf-8",
        )
        openclaw.chmod(0o700)
        raw = config_dict(root, mode=mode)
        raw["telegram"]["enabled"] = True
        raw["openclaw"]["command"] = str(openclaw)
        config = root / "config.json"
        config.write_text(json.dumps(raw), encoding="utf-8")
        config.chmod(0o600)
        return config, capture

    def _run(self, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [str(ROOT / "riskpilot"), "--config", str(config), "--json", *args],
            cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
        )

    def _proposal_callback(self, root: Path) -> tuple[Path, str, str]:
        config, capture = self._config(root)
        made = self._run(config, "demo", "--notify")
        self.assertEqual(made.returncode, 0, made.stderr)
        # Presentation uses native command actions now. Construct legacy sg
        # data independently to prove its compatibility parser still works.
        presentation = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(presentation["blocks"][0]["buttons"][0]["action"]["type"], "command")
        service = SpotGuard(load_settings(config))
        proposal = service.ledger.list_proposals(limit=1)[0]
        private = service.ledger.get_proposal(proposal["id"], include_private=True)
        token = service.signer.approval_token(private["canonical_json"])
        raw = f"sg:approve:{proposal['id']}:{token}"
        reject_raw = f"sg:reject:{proposal['id']}:{token}"
        self.assertTrue(raw.startswith("sg:approve:p-"))
        return config, raw, reject_raw

    def test_fresh_subprocess_callback_approve_replay_and_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            config, raw, _ = self._proposal_callback(Path(directory))
            approved = self._run(config, "callback", "--data", raw,
                "--sender-id", OWNER, "--chat-id", OWNER)
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(json.loads(approved.stdout)["proposal"]["status"], "EXECUTED")
            replay = self._run(config, "callback", "--data", raw,
                "--sender-id", OWNER, "--chat-id", OWNER)
            self.assertNotEqual(replay.returncode, 0)

        with tempfile.TemporaryDirectory() as directory:
            config, _, reject_raw = self._proposal_callback(Path(directory))
            rejected = self._run(config, "callback", "--data", reject_raw,
                "--sender-id", OWNER, "--chat-id", OWNER)
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertEqual(json.loads(rejected.stdout)["proposal"]["status"], "REJECTED")

    def test_expired_and_mode_cross_callbacks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, raw, _ = self._proposal_callback(root)
            import sqlite3
            with sqlite3.connect(root / "state" / "spotguard.db") as db:
                db.execute("UPDATE proposals SET expires_at='2000-01-01T00:00:00Z'")
                db.commit()
            expired = self._run(config, "callback", "--data", raw,
                "--sender-id", OWNER, "--chat-id", OWNER)
            self.assertNotEqual(expired.returncode, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, raw, _ = self._proposal_callback(root)
            config_live, _ = self._config(root, mode="live")
            crossed = self._run(config_live, "callback", "--data", raw,
                "--sender-id", OWNER, "--chat-id", OWNER)
            self.assertNotEqual(crossed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
