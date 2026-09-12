from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path("/home/ubuntu/.openclaw/workspace/.openclaw/extensions/riskpilot-direct-review/index.js")
OPENCLAW_HOOK_RUNNER = Path("/home/ubuntu/.npm-global/lib/node_modules/openclaw/dist/hook-runner-global-BphT2xdR.js")
OPENCLAW_DISPATCH = Path("/home/ubuntu/.npm-global/lib/node_modules/openclaw/dist/dispatch-from-config-D-R0cBMI.js")
REPO_ROOT = Path(__file__).parents[1]
ACTIVE_SKILL = Path("/home/ubuntu/.openclaw/workspace/skills/binance-spotguard")


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
import plugin, {{ cliFailureCategory, createRiskPilotReviewHandler, createRiskPilotReadOnlyHook, extractBeforeDispatchIdentity, normalizeTelegramId, readOnlyRoute, riskPilotInvocation, tradeIntentArgs }} from {json.dumps(PLUGIN.as_uri())};
const owner = {{ senderId: "6146464861", channel: "telegram", channelId: "telegram", to: "telegram:6146464861", isAuthorizedSender: true, senderIsOwner: true,
  config: {{ commands: {{ ownerAllowFrom: ["telegram:6146464861"] }}, channels: {{ telegram: {{ allowFrom: ["6146464861"] }} }} }} }};
let registered;
let interactiveRegistered;
let beforeDispatch;
const pluginApi = {{ config: owner.config, logger: {{ info() {{}} }}, registerCommand(def) {{ registered = def; }}, registerInteractiveHandler(def) {{ interactiveRegistered = def; }}, on(event, handler) {{ if (event === "before_dispatch") beforeDispatch = handler; }} }};
plugin.register(pluginApi);
const fake = async (id) => {{
  if (id !== "c-0123456789ab") throw new Error("unexpected candidate");
  return {scenario};
}};
const handler = createRiskPilotReviewHandler(fake);
const approvalCalls = [];
const approvalHandler = createRiskPilotReviewHandler(fake, undefined, async (action, proposalId, identity) => {{
  approvalCalls.push({{ action, proposalId, identity }});
  return {{ presentation: {{ text: `LIVE ${{action}} ${{proposalId}}` }} }};
}});
const readCalls = [];
const sellCalls = [];
const routeLogs = [];
const readHook = createRiskPilotReadOnlyHook(async (route, identity) => {{
  readCalls.push({{ route, identity }}); return {{ presentation: {{ text: `READ ${{route}}` }} }};
}}, async (text, identity, onDiagnostic) => {{
  onDiagnostic?.({{ phase: "spawn", reached: true }});
  onDiagnostic?.({{ phase: "exit", exitCode: 0, category: "ok" }});
  sellCalls.push({{ text, identity }}); return {{ presentation: {{ text: "SELL PROPOSAL ONLY" }} }};
}}, {{ info: (line) => routeLogs.push(line) }});
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
  live_approve: await approvalHandler({{...owner, isAuthorizedSender: true, args: "live-approve p-0123456789ab"}}),
  live_reject: await approvalHandler({{...owner, isAuthorizedSender: true, args: "live-reject p-0123456789ab"}}),
  live_missing_sender: await approvalHandler({{...owner, isAuthorizedSender: true, senderId: "", args: "live-approve p-0123456789ab"}}),
  live_missing_chat: await approvalHandler({{...owner, isAuthorizedSender: true, to: "", args: "live-approve p-0123456789ab"}}),
  live_wrong_sender: await approvalHandler({{...owner, isAuthorizedSender: true, senderId: "999", args: "live-approve p-0123456789ab"}}),
  live_wrong_chat: await approvalHandler({{...owner, isAuthorizedSender: true, to: "telegram:999", args: "live-approve p-0123456789ab"}}),
  generic_approve: await approvalHandler({{...owner, isAuthorizedSender: true, args: "approve"}}),
  read_balance: await readHook({{ channel: "telegram", senderId: "6146464861", body: "check my live balance" }}, {{ conversationId: "6146464861" }}),
  read_positions: await readHook({{ channel: "telegram", senderId: "6146464861", body: "check all balance and open position" }}, {{ conversationId: "6146464861" }}),
  sell: await readHook({{ channel: "telegram", senderId: "6146464861", body: "sell all XRP" }}, {{ conversationId: "6146464861" }}),
  sell_transport_target: await readHook({{ channel: "telegram", senderId: "6146464861", body: "sell all SOL" }}, {{ conversationId: "telegram:6146464861" }}),
  sell_sender_transport: await readHook({{ channel: "telegram", senderId: "telegram:6146464861", body: "sell all SOL" }}, {{ conversationId: "telegram:6146464861" }}),
  sell_missing_sender: await readHook({{ channel: "telegram", body: "sell all SOL" }}, {{ conversationId: "telegram:6146464861" }}),
  sell_missing_chat: await readHook({{ channel: "telegram", senderId: "6146464861", body: "sell all SOL" }}, {{}}),
  sell_wrong_sender: await readHook({{ channel: "telegram", senderId: "999", body: "sell all SOL" }}, {{ conversationId: "telegram:6146464861" }}),
  sell_wrong_chat: await readHook({{ channel: "telegram", senderId: "6146464861", body: "sell all SOL" }}, {{ conversationId: "telegram:999" }}),
  sell_bad_conversation: await readHook({{ channel: "telegram", senderId: "6146464861", body: "sell all SOL" }}, {{ conversationId: "telegram:direct:6146464861" }}),
  candidate_notification: (await readHook({{ channel: "telegram", senderId: "6146464861", body: "Candidate BTCUSDT c-0123456789ab" }}, {{ conversationId: "telegram:6146464861" }})) ?? null,
  read_bad_sender: await readHook({{ channel: "telegram", senderId: "999", body: "check my live balance" }}, {{ conversationId: "6146464861" }}),
  approvalCalls,
  readCalls, sellCalls, routeLogs,
  before_dispatch_registered: typeof beforeDispatch === "function",
  invocation: riskPilotInvocation(["--json", "live", "balance"], 30000),
  trade_argv: tradeIntentArgs("sell all SOL", {{ senderId: "6146464861", chatId: "6146464861" }}),
  identities: {{
    raw: extractBeforeDispatchIdentity({{ senderId: "6146464861" }}, {{ conversationId: "6146464861" }}, {{ owner: "6146464861", chat: "6146464861" }}),
    transport: extractBeforeDispatchIdentity({{ senderId: "telegram:6146464861" }}, {{ conversationId: "telegram:6146464861" }}, {{ owner: "6146464861", chat: "6146464861" }}),
    badConversation: extractBeforeDispatchIdentity({{ senderId: "6146464861" }}, {{ conversationId: "telegram:direct:6146464861" }}, {{ owner: "6146464861", chat: "6146464861" }}),
  }},
  normalize: {{ numeric: normalizeTelegramId("6146464861", true), prefixed: normalizeTelegramId("telegram:6146464861", true), number: normalizeTelegramId(6146464861, true), invalid: normalizeTelegramId("telegram:direct:6146464861", true) }},
  routes: {{
    balance: readOnlyRoute("check my live balance"),
    positions: readOnlyRoute("check all balance and open position"),
    exactBalance: readOnlyRoute("/spot live balance"),
    exactPositions: readOnlyRoute("/spot live positions"),
    genericApprove: readOnlyRoute("approve"),
  }},
  failureCategories: {{
    security: cliFailureCategory({{ code: 2 }}, {{ type: "SecurityError" }}),
    policy: cliFailureCategory({{ code: 2 }}, {{ type: "PolicyError" }}),
    validation: cliFailureCategory({{ code: 2 }}, {{ type: "ValueError" }}),
    notification: cliFailureCategory({{ code: 2 }}, {{ type: "TelegramError" }}),
    argparse: cliFailureCategory({{ code: 2 }}, null),
  }},
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
        self.assertEqual(result["live_approve"]["text"], "LIVE live-approve p-0123456789ab")
        self.assertEqual(result["live_reject"]["text"], "LIVE live-reject p-0123456789ab")
        self.assertFalse(result["live_approve"]["continueAgent"])
        self.assertFalse(result["live_reject"]["continueAgent"])
        self.assertEqual(result["approvalCalls"], [
            {"action": "live-approve", "proposalId": "p-0123456789ab",
             "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
            {"action": "live-reject", "proposalId": "p-0123456789ab",
             "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
        ])
        for key in ("live_missing_sender", "live_missing_chat", "live_wrong_sender", "live_wrong_chat"):
            self.assertTrue(result[key]["isError"], key)
        # Plain text cannot accidentally become a LIVE action.
        self.assertTrue(result["generic_approve"]["isError"])
        self.assertEqual(len(result["approvalCalls"]), 2)
        self.assertEqual(result["routes"], {
            "balance": "balance", "positions": "positions", "exactBalance": "balance",
            "exactPositions": "positions", "genericApprove": None,
        })
        self.assertEqual(result["failureCategories"], {
            "security": "riskpilot_security_error",
            "policy": "riskpilot_policy_error",
            "validation": "riskpilot_validation_error",
            "notification": "riskpilot_notification_error",
            "argparse": "riskpilot_argparse_error",
        })
        self.assertEqual(result["read_balance"]["text"], "READ balance")
        self.assertEqual(result["read_positions"]["text"], "READ positions")
        self.assertTrue(result["read_balance"]["handled"])
        self.assertTrue(result["read_positions"]["handled"])
        self.assertEqual(result["sell"]["text"], "SELL PROPOSAL ONLY")
        self.assertEqual(result["sell_transport_target"]["text"], "SELL PROPOSAL ONLY")
        self.assertEqual(result["sell_sender_transport"]["text"], "SELL PROPOSAL ONLY")
        for key in ("sell_missing_sender", "sell_missing_chat", "sell_wrong_sender", "sell_wrong_chat", "sell_bad_conversation"):
            self.assertTrue(result[key]["handled"], key)
            self.assertTrue(result[key]["isError"], key)
        self.assertIsNone(result["candidate_notification"])
        self.assertTrue(result["before_dispatch_registered"])
        self.assertTrue(result["read_bad_sender"]["isError"])
        self.assertEqual(result["readCalls"], [
            {"route": "balance", "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
            {"route": "positions", "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
        ])
        self.assertEqual(result["sellCalls"], [
            {"text": "sell all XRP", "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
            {"text": "sell all SOL", "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
            {"text": "sell all SOL", "identity": {"senderId": "6146464861", "chatId": "6146464861"}},
        ])
        # The subprocess boundary is mocked above. This exact argv proves a
        # clear SELL only creates a deferred trade-intent proposal path; it
        # cannot submit a Binance order in this regression test.
        self.assertEqual(result["trade_argv"], [
            "--config", "/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os/config.json",
            "--json", "trade-intent", "--text", "sell all SOL",
            "--sender-id", "6146464861", "--chat-id", "6146464861", "--notify",
        ])
        self.assertEqual(result["normalize"], {
            "numeric": "6146464861", "prefixed": "6146464861",
            "number": "6146464861", "invalid": None,
        })
        self.assertEqual(result["identities"]["raw"]["identity"], {"senderId": "6146464861", "chatId": "6146464861"})
        self.assertEqual(result["identities"]["transport"]["identity"], {"senderId": "6146464861", "chatId": "6146464861"})
        self.assertEqual(result["identities"]["badConversation"]["reason"], "malformed_trusted_chat_metadata")
        trade_logs = [line for line in result["routeLogs"] if "route=trade-intent" in line]
        self.assertTrue(any("sender_field=event.senderId" in line and "chat_field=context.conversationId" in line for line in trade_logs))
        self.assertTrue(any("cli_spawn=true" in line for line in trade_logs))
        self.assertTrue(any("cli_exit=0" in line and "cli_category=ok" in line for line in trade_logs))
        self.assertEqual(result["invocation"], {
            "file": "/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os/riskpilot",
            "args": ["--json", "live", "balance"],
            "cwd": "/home/ubuntu/.openclaw/workspace/tools/spotguard-agent-os",
            "timeout": 30000, "maxBuffer": 2 * 1024 * 1024, "shell": False,
        })

    def test_no_trade_and_failure_do_not_create_proposal(self) -> None:
        no_trade = self._run('{review_decision:"NO_TRADE",market_review:{reason:"not coherent"}}')
        failed = self._run('(() => { throw new Error("review failure"); })()')
        self.assertIn("NO_TRADE", no_trade["authorized"]["text"])
        self.assertIn("No proposal", failed["authorized"]["text"])

    def test_approve_suppresses_llm_fallback_after_existing_notification(self) -> None:
        result = self._run('{review_decision:"APPROVE",proposal:{id:"p-1"},notification:{delivered:true}}')
        self.assertTrue(result["authorized"]["suppressReply"])

    def test_presentation_button_uses_native_command(self) -> None:
        from spotguard.telegram import candidate_message
        _, buttons = candidate_message({"id": "c-0123456789ab", "symbol": "BTCUSDT", "interval": "15m", "score": 95, "price": 1})
        self.assertEqual(buttons[0]["command"], "/binance_spotguard review c-0123456789ab")

    def test_installed_skill_routing_files_match_canonical_source(self) -> None:
        # `openclaw skills install --force` is the supported sync mechanism.
        for relative in ("SKILL.md", "references/workflow.md"):
            self.assertEqual(
                (REPO_ROOT / "skills/binance-spotguard" / relative).read_text(encoding="utf-8"),
                (ACTIVE_SKILL / relative).read_text(encoding="utf-8"),
                relative,
            )

    def test_external_plugin_hook_is_claimed_by_real_openclaw_dispatcher(self) -> None:
        """Use OpenClaw's installed typed-hook runner, not a hand-rolled fake.

        Both inputs deliberately lack sender metadata, so the extension returns
        a fail-closed handled result before any RiskPilot subprocess or Binance
        call can occur. The simulated downstream agent counter must remain zero.
        """
        self.assertTrue(OPENCLAW_HOOK_RUNNER.is_file())
        self.assertTrue(OPENCLAW_DISPATCH.is_file())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = root / "loader.mjs"
            loader.write_text(
                'export async function resolve(s,c,n) {\n'
                ' if (s === "openclaw/plugin-sdk/plugin-entry") return {url: "data:text/javascript,export function definePluginEntry(x){return x}", shortCircuit: true};\n'
                ' return n(s,c);\n'
                '}\n', encoding="utf-8"
            )
            script = f"""
import plugin from {json.dumps(PLUGIN.as_uri())};
const hookModule = await import({json.dumps(OPENCLAW_HOOK_RUNNER.as_uri())});
const initialize = Object.values(hookModule).find((value) => value?.name === "initializeGlobalHookRunner");
const getRunner = Object.values(hookModule).find((value) => value?.name === "getGlobalHookRunner");
const reset = Object.values(hookModule).find((value) => value?.name === "resetGlobalHookRunner");
if (!initialize || !getRunner || !reset) throw new Error("installed OpenClaw hook runner exports changed");
let registeredHook;
plugin.register({{
  logger: {{ info() {{}} }},
  registerCommand() {{}},
  registerInteractiveHandler() {{}},
  on(name, handler) {{ if (name === "before_dispatch") registeredHook = handler; }},
}});
if (typeof registeredHook !== "function") throw new Error("extension did not register before_dispatch");
initialize({{
  hooks: [],
  typedHooks: [{{ pluginId: "riskpilot-direct-review", hookName: "before_dispatch", handler: registeredHook }}],
  plugins: [{{ id: "riskpilot-direct-review", status: "loaded" }}],
  trustedToolPolicies: [],
}});
const runner = getRunner();
let downstreamAgentCalls = 0;
async function dispatch(event) {{
  const result = await runner.runBeforeDispatch(event, {{ channelId: "telegram", conversationId: "telegram:6146464861" }});
  if (!result?.handled) downstreamAgentCalls += 1;
  return result;
}}
const balance = await dispatch({{ channel: "telegram", content: "check my live balance", body: "check my live balance" }});
const sell = await dispatch({{ channel: "telegram", content: "sell all SOL", body: "sell all SOL" }});
console.log(JSON.stringify({{ balance, sell, downstreamAgentCalls }}));
reset();
"""
            result = subprocess.run(
                ["node", "--experimental-loader", str(loader), "--input-type=module", "-e", script],
                capture_output=True, text=True, check=True,
            )
        observed = json.loads(result.stdout)
        self.assertTrue(observed["balance"]["handled"])
        self.assertTrue(observed["sell"]["handled"])
        self.assertEqual(observed["downstreamAgentCalls"], 0)
        # Guard the installed dispatch contract that returns before the model
        # path whenever a typed before_dispatch hook reports handled.
        dispatch_source = OPENCLAW_DISPATCH.read_text(encoding="utf-8")
        self.assertIn("if (beforeDispatchResult?.handled)", dispatch_source)
        self.assertIn('reason: "before_dispatch_handled"', dispatch_source)

    def test_external_extension_manifest_is_startup_activated(self) -> None:
        manifest = json.loads((PLUGIN.parent / "openclaw.plugin.json").read_text(encoding="utf-8"))
        config = json.loads(Path("/home/ubuntu/.openclaw/openclaw.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "riskpilot-direct-review")
        self.assertTrue(manifest["activation"]["onStartup"])
        self.assertTrue(config["plugins"]["entries"][manifest["id"]]["enabled"])


if __name__ == "__main__":
    unittest.main()
