from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).parents[1] / "extensions/riskpilot-direct-review/index.js"
CONFIG = Path("/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os/config.json")


class TelegramReviewSkippedTests(unittest.TestCase):
    def test_explicit_skipped_review_is_successful_but_actual_failure_stays_failed(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        owner = str(config["openclaw"]["telegram_owner_id"])
        chat = str(config["telegram"]["chat_id"])
        with tempfile.TemporaryDirectory() as directory:
            loader = Path(directory) / "loader.mjs"
            loader.write_text('export async function resolve(s,c,n) { if (s === "openclaw/plugin-sdk/plugin-entry") return {url: "data:text/javascript,export function definePluginEntry(x){return x}", shortCircuit: true}; return n(s,c); }', encoding="utf-8")
            script = f'''import {{ createRiskPilotReviewHandler }} from {json.dumps(PLUGIN.as_uri())};
const owner=process.env.RP_OWNER;
const context={{senderId:owner,channel:"telegram",channelId:"telegram",to:`telegram:${{process.env.RP_CHAT}}`,isAuthorizedSender:true}};
const skipped=createRiskPilotReviewHandler(async()=>({{review_decision:"APPROVE",proposal:null,proposal_status:"SKIPPED_NOT_EXECUTION_READY",presentation:{{text:"RiskPilot AI REVIEW completed. LIVE proposal was not created."}}}}));
const failed=createRiskPilotReviewHandler(async()=>({{review_decision:"APPROVE",proposal:null}}));
console.log(JSON.stringify({{skipped:await skipped({{...context,args:"review c-0123456789ab"}}),failed:await failed({{...context,args:"review c-0123456789ab"}})}}));'''
            done = subprocess.run(["node", "--experimental-loader", str(loader), "--input-type=module", "-e", script], text=True, capture_output=True, check=True, env={**os.environ, "RP_OWNER": owner, "RP_CHAT": chat})
        result = json.loads(done.stdout)
        self.assertIn("AI REVIEW completed", result["skipped"]["text"])
        self.assertNotIn("FAILED", result["skipped"]["text"])
        self.assertFalse(result["skipped"]["continueAgent"])
        self.assertIn("FAILED", result["failed"]["text"])
