from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).parents[1] / "extensions/riskpilot-direct-review/index.js"


class ExtensionReviewTimeoutTests(unittest.TestCase):
    def test_review_outer_timeout_is_derived_from_inner_timeout_and_cleanup_margin(self) -> None:
        observed = self._run("""
console.log(JSON.stringify({
  configured: reviewOuterTimeoutMilliseconds(240),
  minimum: reviewOuterTimeoutMilliseconds(30),
}));
""")
        # 240 seconds of Codex work + 3 seconds bridge cleanup + 27 seconds
        # bounded CLI/result overhead.  The inner timeout remains unchanged.
        self.assertEqual(observed, {"configured": 270_000, "minimum": 60_000})

    def test_outer_cleanup_kills_only_the_owned_pid_tree_and_next_review_can_start(self) -> None:
        observed = self._run("""
const sleeper = "setInterval(() => {}, 1000)";
const parentProgram = `const {spawn}=require('node:child_process'); const child=spawn(process.execPath,['-e',${JSON.stringify(sleeper)}],{stdio:'ignore'}); console.log(child.pid); setInterval(()=>{},1000);`;
const parent = spawn(process.execPath, ["-e", parentProgram], {stdio:["ignore", "pipe", "ignore"]});
const childPid = Number((await once(parent.stdout, "data"))[0].toString().trim());
const unrelated = spawn(process.execPath, ["-e", sleeper], {stdio:"ignore"});
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
try {
  await terminateOwnedRiskPilotInvocation(parent.pid);
  await new Promise((resolve) => setTimeout(resolve, 150));
  const next = spawn(process.execPath, ["-e", "process.stdout.write('next-review')"], {stdio:["ignore", "pipe", "ignore"]});
  const nextOutput = (await once(next.stdout, "data"))[0].toString();
  console.log(JSON.stringify({ parentAlive: alive(parent.pid), childAlive: alive(childPid), unrelatedAlive: alive(unrelated.pid), nextOutput }));
} finally {
  if (alive(unrelated.pid)) unrelated.kill("SIGKILL");
}
""")
        self.assertFalse(observed["parentAlive"])
        self.assertFalse(observed["childAlive"])
        self.assertTrue(observed["unrelatedAlive"])
        self.assertEqual(observed["nextOutput"], "next-review")

    def _run(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            loader = Path(directory) / "loader.mjs"
            loader.write_text(
                'export async function resolve(s,c,n) { if (s === "openclaw/plugin-sdk/plugin-entry") return {url: "data:text/javascript,export function definePluginEntry(x){return x}", shortCircuit: true}; return n(s,c); }',
                encoding="utf-8",
            )
            script = f"""
import {{ spawn }} from "node:child_process";
import {{ once }} from "node:events";
import {{ reviewOuterTimeoutMilliseconds, terminateOwnedRiskPilotInvocation }} from {json.dumps(PLUGIN.as_uri())};
{body}
"""
            completed = subprocess.run(
                ["node", "--experimental-loader", str(loader), "--input-type=module", "-e", script],
                text=True,
                capture_output=True,
                check=True,
                timeout=20,
            )
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
