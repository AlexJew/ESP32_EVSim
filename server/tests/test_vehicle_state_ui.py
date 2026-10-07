import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs the three page scripts in Node against the stub DOM with a scripted fetch. Steps: ["select", "B"] picks an option
# in the state dropdown, ["telemetry", telemetry] changes what /api/telemetry returns and polls it, ["wait", ms] waits.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { elements, document } = installDom();
const scenario = JSON.parse(process.argv[2]);
const posts = [];
const respond = (ok, status, body, delay = 0) =>
  new Promise((resolve) => setTimeout(() => resolve({ ok, status, json: async () => body }), delay));
global.fetch = (url, options) => {
  if (options && options.method === "POST") {
    posts.push(JSON.parse(options.body));
    const post = scenario.post;
    return respond(post.ok, post.ok ? 200 : post.status, post.body, post.delay || 0);
  }
  if (url === "/api/charger/status") return respond(true, 200, scenario.charger);
  if (url === "/api/telemetry") return respond(true, 200, scenario.telemetry);
  return Promise.reject(new Error("not served in this test: " + url));
};
const code = ["chart.js", "dashboard.js", "app.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.page = { pollTelemetry };");
const select = document.getElementById("vehicle-state");
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const snapshot = () => {
  const pill = document.getElementById("command-status");
  return { posts: posts.slice(), select: { value: select.value, disabled: select.disabled === true },
           pill: { text: pill.textContent, kind: pill.dataset.kind, title: pill.title } };
};
(async () => {
  await wait(30);
  const log = [snapshot()];
  for (const [kind, value] of scenario.steps) {
    if (kind === "select") { select.value = value; select.listeners.change(); }
    if (kind === "telemetry") { scenario.telemetry = value; page.pollTelemetry(); }
    if (kind === "wait") await wait(value);
    log.push(snapshot());
  }
  console.log(JSON.stringify(log));
})();
"""

CHARGER = {
    "status": "ok", "link": "ok", "error": None, "paused": False, "operator_below_set": False,
    "set_limit_a": {"value": 10, "status": "ok", "updated_at": None},
    "operator_limit_a": {"value": 16, "status": "ok", "updated_at": None},
    "installation_limit_a": {"value": 16, "status": "ok", "updated_at": None},
    "signalled_current_a": {"value": 10, "status": "ok", "updated_at": None},
    "meter_current_a": {"value": {"L1": 11.7, "L2": 11.6, "L3": 12.0}, "status": "ok", "updated_at": None},
    "meter_voltage_v": {"value": {"L1": 231, "L2": 232, "L3": 233}, "status": "ok", "updated_at": None},
    "meter_power_w": {"value": 600, "status": "ok", "updated_at": None},
    "vehicle_state": {"value": "C", "status": "ok", "updated_at": None},
    "charger_status": {"value": "Charging", "status": "ok", "updated_at": None},
    "desired_limit_a": 10, "restored_at": None,
}


def telemetry(state="C", connected=True):
    ok = {"value": 5000.0, "status": "ok", "updated_at": None}
    gone = {"value": None, "status": "unavailable", "updated_at": None}
    return {
        "mqtt": {"connected": connected},
        "board": {"setpoint_w": ok, "state": state, "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": ok, "status": "ok"},
        "server": {"target_dc_w": ok, "mode": "capped", "inverter_state": 4},
        "inverter": {name: gone for name in ("dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v")},
    }


ACCEPTED = {"ok": True, "body": {"success": True, "state": "B", "reply": "state C -> B"}}
REFUSED = {"ok": False, "status": 409, "body": {"success": False, "error": "state B must be asserted first"}}
NO_ANSWER = {"ok": False, "status": 503, "body": {"success": False, "error": "The board did not answer."}}


def run(steps, post=ACCEPTED, board=None):
    scenario = {"charger": copy.deepcopy(CHARGER), "telemetry": board or telemetry(), "post": post, "steps": steps}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StateDropdownTest(unittest.TestCase):
    def test_the_dropdown_starts_on_the_boards_state_and_enabled(self):
        first = run([])[0]
        self.assertEqual((first["select"], first["posts"]), ({"value": "C", "disabled": False}, []))

    def test_with_the_broker_down_it_is_empty_and_disabled(self):
        first = run([], board=telemetry(connected=False))[0]
        self.assertEqual(first["select"], {"value": "", "disabled": True})

    def test_choosing_a_state_sends_it_once_and_reports_validated(self):
        log = run([("select", "B"), ("wait", 60)])
        self.assertEqual(log[-1]["posts"], [{"state": "B"}])
        self.assertEqual((log[-1]["pill"]["text"], log[-1]["pill"]["kind"]), ("Validated", "ok"))
        self.assertFalse(log[-1]["select"]["disabled"])

    def test_the_dropdown_is_off_while_the_command_is_in_flight(self):
        slow = {"ok": True, "delay": 60, "body": ACCEPTED["body"]}
        log = run([("select", "B"), ("wait", 20), ("wait", 100)], post=slow)
        self.assertEqual(log[1]["select"]["disabled"], True)                    # right after the choice
        self.assertEqual(log[1]["pill"]["text"], "Sending…")
        self.assertEqual(log[-1]["select"]["disabled"], False)

    def test_a_poll_while_pending_does_not_move_the_dropdown(self):
        slow = {"ok": True, "delay": 80, "body": ACCEPTED["body"]}
        log = run([("select", "B"), ("telemetry", telemetry("C")), ("wait", 30)], post=slow)
        self.assertEqual(log[-1]["select"], {"value": "B", "disabled": True})

    def test_a_refusal_says_not_validated_with_the_reason_and_returns_to_the_boards_state(self):
        log = run([("select", "B"), ("wait", 60)], post=REFUSED)
        last = log[-1]
        self.assertEqual((last["pill"]["text"], last["pill"]["kind"]), ("Not validated", "error"))
        self.assertIn("must be asserted first", last["pill"]["title"])
        self.assertEqual(last["select"], {"value": "C", "disabled": False})

    def test_no_answer_from_the_board_is_not_validated_and_restores_the_dropdown(self):
        last = run([("select", "A"), ("wait", 60)], post=NO_ANSWER)[-1]
        self.assertEqual((last["pill"]["text"], last["select"]["value"]), ("Not validated", "C"))
        self.assertIn("did not answer", last["pill"]["title"])

    def test_after_an_accepted_command_the_next_poll_shows_the_new_state(self):
        log = run([("select", "B"), ("wait", 60), ("telemetry", telemetry("B")), ("wait", 40)])
        self.assertEqual(log[-1]["select"], {"value": "B", "disabled": False})

    def test_choosing_the_empty_option_sends_nothing(self):
        log = run([("select", ""), ("wait", 40)])
        self.assertEqual(log[-1]["posts"], [])


if __name__ == "__main__":
    unittest.main()
