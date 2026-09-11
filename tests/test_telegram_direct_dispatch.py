from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path("/home/ubuntu/.openclaw/workspace/.openclaw/extensions/riskpilot-direct-review/index.js")


class TelegramDirectDispatchTests(unittest.TestCase):
    def _run(self, scenario: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = root / "loader.mjs"
            loader.write_text(
                'export async function resolve(s,c,n) {\n'
                ' if (s === "openclaw/plugin-sdk/plugin-entry") return {url: "data:text/javascript,export function definePluginEntry(x){return x}", shortCircuit: true};\n'
                ' return n(s,c);\n}\n', encoding="utf-8"
            )
            script = f"""
import plugin, {{ createRiskPilotReviewHandler }} from {json.dumps(PLUGIN.as_uri())};
const owner = {{ senderId: "6146464861", channel: "telegram", channelId: "telegram", to: "telegram:6146464861", senderIsOwner: true,
  config: {{ commands: {{ ownerAllowFrom: ["telegram:6146464861"] }}, channels: {{ telegram: {{ allowFrom: ["6146464861"] }} }} }} }};
let registered;
let interactiveRegistered;
const pluginApi = {{ config: owner.config, logger: {{ info() {{}} }}, registerCommand(def) {{ registered = def; }}, registerInteractiveHandler(def) {{ interactiveRegistered = def; }} }};
plugin.register(pluginApi);
const fake = async (id) => {{
  if (id !== "c-0123456789ab") throw new Error("unexpected candidate");
  return {scenario};
}};
const handler = createRiskPilotReviewHandler(fake);
const replies = [];
const interactiveHandler = interactiveRegistered.handler;
const interactiveResult = await interactiveHandler({{
  channel: "telegram", senderId: "6146464861", auth: {{ isAuthorizedSender: true }},
  callback: {{ payload: "c-0123456789ab", chatId: "6146464861" }},
  respond: {{ reply: async (value) => replies.push(value.text), clearButtons: async () => {{}} }}
}});
const unauthorizedInteractive = await interactiveHandler({{
  channel: "telegram", senderId: "999", auth: {{ isAuthorizedSender: false }},
  callback: {{ payload: "c-0123456789ab", chatId: "999" }},
  respond: {{ reply: async (value) => replies.push(value.text), clearButtons: async () => {{}} }}
}});
const cases = {{
 authorized: await handler({{...owner, args: "review c-0123456789ab"}}),
 extra_args: await handler({{...owner, args: "review c-0123456789ab extra"}}),
 bad_sender: await handler({{...owner, senderId: "999" , args: "review c-0123456789ab"}}),
 bad_chat: await handler({{...owner, to: "telegram:999", args: "review c-0123456789ab"}}),
 bad_id: await handler({{...owner, args: "review ../../etc/passwd"}}),
 registered_name: registered.name,
  registered_channels: registered.channels,
  interactive_namespace: interactiveRegistered.namespace,
  interactive_result: interactiveResult,
  unauthorized_interactive: unauthorizedInteractive,
  replies,
}};
console.log(JSON.stringify(cases));
"""
            result = subprocess.run(
                ["node", "--experimental-loader", str(loader), "--input-type=module", "-e", script],
                capture_output=True, text=True, check=True,
            )
            return json.loads(result.stdout)

    def test_authorized_reject_dispatch_and_validation(self) -> None:
        result = self._run('{review_decision:"REJECT",market_review:{reason:"fresh data rejected"}}')
        self.assertIn("REJECT", result["authorized"]["text"])
        self.assertTrue(result["extra_args"]["isError"])
        self.assertTrue(result["bad_sender"]["isError"])
        self.assertTrue(result["bad_chat"]["isError"])
        self.assertTrue(result["bad_id"]["isError"])
        self.assertEqual(result["registered_name"], "binance_spotguard")
        self.assertEqual(result["registered_channels"], ["telegram"])
        self.assertEqual(result["interactive_namespace"], "riskpilot-review")
        self.assertTrue(result["interactive_result"]["handled"])
        self.assertTrue(result["unauthorized_interactive"]["handled"])
        self.assertGreaterEqual(len(result["replies"]), 2)

    def test_no_trade_and_failure_do_not_create_proposal(self) -> None:
        no_trade = self._run('{review_decision:"NO_TRADE",market_review:{reason:"not coherent"}}')
        failed = self._run('(() => { throw new Error("review failure"); })()')
        self.assertIn("NO_TRADE", no_trade["authorized"]["text"])
        self.assertIn("No proposal", failed["authorized"]["text"])

    def test_approve_suppresses_llm_fallback_after_existing_notification(self) -> None:
        result = self._run('{review_decision:"APPROVE",proposal:{id:"p-1"},notification:{delivered:true}}')
        self.assertTrue(result["authorized"]["suppressReply"])

    def test_presentation_button_uses_callback_payload(self) -> None:
        from spotguard.telegram import candidate_message
        _, buttons = candidate_message({"id": "c-0123456789ab", "symbol": "BTCUSDT", "interval": "15m", "score": 95, "price": 1})
        self.assertEqual(buttons[0]["value"], "riskpilot-review:c-0123456789ab")
        self.assertNotIn("command", buttons[0])


if __name__ == "__main__":
    unittest.main()
