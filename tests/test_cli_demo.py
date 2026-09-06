from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from spotguard.cli import main

from tests.helpers import write_config


class CliDemoTests(unittest.TestCase):
    def test_end_to_end_paper_demo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--config", str(config), "--json", "demo", "--auto-approve"])
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["execution"]["status"], "EXECUTED")
            self.assertTrue(payload["execution"]["execution_summary"]["simulated"])


if __name__ == "__main__":
    unittest.main()
