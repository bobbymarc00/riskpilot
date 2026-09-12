from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).parents[1] / "extensions/riskpilot-direct-review/index.js"
CONFIG = Path("/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os/config.json")


class TelegramNaturalBuyTests(unittest.TestCase):
    def _run(self) -> dict:
        settings = json.loads(CONFIG.read_text(encoding="utf-8"))
        owner = str(settings["openclaw"]["telegram_owner_id"])
        chat = str(settings["telegram"]["chat_id"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = root / "loader.mjs"
            loader.write_text(
                'export async function resolve(s,c,n) {\n'
                ' if (s === "openclaw/plugin-sdk/plugin-entry") return {url: "data:text/javascript,export function definePluginEntry(x){return x}", shortCircuit: true};\n'
                ' return n(s,c);\n}\n', encoding="utf-8"
            )
            script = f"""
import {{ createRiskPilotReadOnlyHook }} from {json.dumps(PLUGIN.as_uri())};
const owner = process.env.RP_OWNER;
const chat = process.env.RP_CHAT;
const trades = [];
const reads = [];
let modelDispatches = 0;
const hook = createRiskPilotReadOnlyHook(
  async (route) => {{ reads.push(route); return {{ presentation: {{ text: `READ ${{route}}` }} }}; }},
  async (text) => {{ trades.push(text); return {{ presentation: {{ text: `REFUSED ${{text}}` }} }}; }},
);
const refusalHook = createRiskPilotReadOnlyHook(
  async () => {{ throw new Error("read route must not run"); }},
  async () => ({{ type: "SecurityError", error: "live execution readiness checks have not all passed", presentation: {{ text: "Request refused" }} }}),
);
async function dispatch(body) {{
  const result = await hook({{ channel: "telegram", senderId: owner, body }}, {{ conversationId: `telegram:${{chat}}` }});
  if (!result?.handled) modelDispatches += 1;
  return result ?? null;
}}
const buys = {json.dumps(["buy SOL 6", "buy SOL 6usd", "buy SOL 6 usdt", "buy SOLUSDT 6", "buy SOLUSDT 6usd", "buy PEOPLEUSDT 6", "buy PEOPLEUSDT 6usd"])};
const negatives = {json.dumps(["buy it", "buy SOL", "buy SOL -1", "buy SOL 0", "buy SOL .5", "buy SOL NaN", "buy SOL Infinity", "should I buy SOL 6", "approve", "SOL buy 6"])};
const refusal = await refusalHook({{ channel: "telegram", senderId: owner, body: "buy PEOPLEUSDT 6" }}, {{ conversationId: `telegram:${{chat}}` }});
const results = {{ buys: [], negatives: [], refusal, sell: await dispatch("sell all SOL"), balance: await dispatch("check my live balance"), positions: await dispatch("check all balance and open position"), radar: await dispatch("radar") }};
for (const text of buys) results.buys.push(await dispatch(text));
for (const text of negatives) results.negatives.push(await dispatch(text));
console.log(JSON.stringify({{ results, trades, reads, modelDispatches }}));
"""
            environment = dict(os.environ, RP_OWNER=owner, RP_CHAT=chat)
            completed = subprocess.run(
                ["node", "--experimental-loader", str(loader), "--input-type=module", "-e", script],
                text=True, capture_output=True, check=True, env=environment,
            )
        return json.loads(completed.stdout)

    def test_bounded_buy_syntax_is_consumed_by_existing_trade_intent_path(self) -> None:
        observed = self._run()
        self.assertEqual(observed["trades"], [
            "sell all SOL", "buy SOL 6", "buy SOL 6usd", "buy SOL 6 usdt",
            "buy SOLUSDT 6", "buy SOLUSDT 6usd", "buy PEOPLEUSDT 6",
            "buy PEOPLEUSDT 6usd",
        ])
        self.assertTrue(all(item["handled"] for item in observed["results"]["buys"]))
        self.assertTrue(observed["results"]["refusal"]["handled"])
        self.assertIn("live execution readiness checks have not all passed", observed["results"]["refusal"]["text"])
        # The unmatched radar plus nine negative phrases are the only turns
        # eligible for normal dispatch; every bounded BUY was consumed here.
        self.assertEqual(observed["modelDispatches"], 11)

    def test_existing_fast_routes_remain_claimed(self) -> None:
        observed = self._run()
        self.assertEqual(observed["trades"][0], "sell all SOL")
        self.assertEqual(observed["reads"], ["balance", "positions"])
        self.assertTrue(observed["results"]["sell"]["handled"])
        self.assertTrue(observed["results"]["balance"]["handled"])
        self.assertTrue(observed["results"]["positions"]["handled"])
        self.assertIsNone(observed["results"]["radar"])
        self.assertTrue(all(item is None for item in observed["results"]["negatives"]))
