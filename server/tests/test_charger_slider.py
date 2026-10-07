import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs the three page scripts in Node against the stub DOM with a scripted fetch, then plays the steps:
# ["input", v] moves the slider, ["change"] releases it, ["poll"] presses "Refresh now", ["wait", ms] waits.
# After every step it logs what the page shows and which limits were POSTed.
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
    posts.push(JSON.parse(options.body).current_limit_a);
    const post = scenario.post;
    if (post.ok && post.body.set_limit_a !== undefined) scenario.status.set_limit_a.value = post.body.set_limit_a;
    return respond(post.ok, post.ok ? 200 : 503, post.body, post.delay || 0);
  }
  if (url === "/api/charger/status") return respond(true, 200, scenario.status);
  return Promise.reject(new Error("not served in this test: " + url));
};
const code = ["chart.js", "dashboard.js", "app.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code);
const slider = document.getElementById("current-limit");
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const text = (id) => document.getElementById(id).textContent;
const snapshot = () => {
  const pill = document.getElementById("command-status");
  const warning = document.getElementById("operator-warning");
  return {
    posts: posts.slice(), slider: slider.value, sliderMax: slider.max, limit: text("limit-value"),
    pill: { text: pill.textContent, kind: pill.dataset.kind, title: pill.title },
    state: text("vehicle-state"), signalled: text("signalled-current"), charger: text("meter-charger"),
    installation: text("installation-limit"), warning: { hidden: warning.hidden, text: warning.textContent },
    ticks: { min: text("tick-min"), max: text("tick-max"), mid: text("tick-mid"), midHidden: document.getElementById("tick-mid").hidden },
  };
};
(async () => {
  await wait(30);
  const log = [snapshot()];
  for (const [kind, value] of scenario.steps) {
    if (kind === "input") { slider.value = String(value); slider.listeners.input(); }
    if (kind === "change") slider.listeners.change();
    if (kind === "poll") document.getElementById("refresh").listeners.click();
    if (kind === "wait") await wait(value);
    log.push(snapshot());
  }
  console.log(JSON.stringify(log));
})();
"""

STATUS = {
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
OK = {"ok": True, "body": {"success": True, "set_limit_a": 14}}
REFUSED = {"ok": False, "body": {"success": False, "error": "Modbus write was not confirmed (boom)."}}


def run(steps, status=None, post=OK):
    scenario = {"status": status or copy.deepcopy(STATUS), "post": post, "steps": steps}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def status_with(**changes):
    status = copy.deepcopy(STATUS)
    for key, value in changes.items():
        status[key] = value
    return status


def reading(value):
    return {"value": value, "status": "ok", "updated_at": None}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SliderTest(unittest.TestCase):
    def test_the_page_shows_what_the_charger_reports(self):
        first = run([])[0]
        self.assertEqual((first["slider"], first["limit"]), ("10", "10 A"))
        self.assertEqual((first["signalled"], first["charger"]), ("10 A", "11.8 A / 232 V / 600 W"))   # means of the phases
        self.assertEqual(first["installation"], "Installation limit: 16 A")
        self.assertEqual(first["posts"], [])

    def test_the_ticks_mark_the_ends_and_the_current_value(self):
        first = run([])[0]
        self.assertEqual(first["ticks"], {"min": "6 A", "max": "16 A", "mid": "10 A", "midHidden": False})

    def test_the_middle_tick_hides_when_the_value_sits_on_an_end(self):
        at_max = run([], status_with(set_limit_a=reading(16)))[0]
        self.assertTrue(at_max["ticks"]["midHidden"])

    def test_the_slider_tops_out_at_the_installation_limit(self):
        first = run([], status_with(installation_limit_a=reading(13)))[0]
        self.assertEqual(first["sliderMax"], "13")

    def test_a_charger_paused_at_zero_reads_0_a_and_the_slider_rests_at_its_minimum(self):
        first = run([], status_with(set_limit_a=reading(0)))[0]
        self.assertEqual((first["limit"], first["slider"], first["ticks"]["midHidden"]), ("0 A", "6", True))

    def test_dragging_shows_the_value_without_sending_and_a_poll_does_not_move_it(self):
        log = run([("input", 14), ("poll", None), ("wait", 40)])
        self.assertEqual((log[-1]["slider"], log[-1]["limit"], log[-1]["posts"]), ("14", "14 A", []))

    def test_releasing_sends_one_value_and_reports_validated(self):
        log = run([("input", 14), ("change", None), ("wait", 60)])
        self.assertEqual(log[-1]["posts"], [14])
        self.assertEqual((log[-1]["pill"]["text"], log[-1]["pill"]["kind"]), ("Validated", "ok"))
        self.assertEqual((log[-1]["slider"], log[-1]["limit"]), ("14", "14 A"))

    def test_a_refused_write_says_not_validated_with_the_reason_in_the_tooltip_and_restores_the_slider(self):
        log = run([("input", 14), ("change", None), ("wait", 60)], post=REFUSED)
        last = log[-1]
        self.assertEqual((last["pill"]["text"], last["pill"]["kind"]), ("Not validated", "error"))
        self.assertIn("boom", last["pill"]["title"])
        self.assertEqual((last["slider"], last["limit"], last["posts"]), ("10", "10 A", [14]))

    def test_a_value_below_6_is_refused_in_the_page_without_a_request(self):
        log = run([("input", 5), ("change", None), ("wait", 40)])
        last = log[-1]
        self.assertEqual((last["posts"], last["pill"]["text"]), ([], "Not validated"))
        self.assertEqual((last["slider"], last["limit"]), ("10", "10 A"))

    def test_a_value_chosen_while_a_write_is_in_flight_is_sent_after_it(self):
        slow = {"ok": True, "delay": 50, "body": {"success": True, "set_limit_a": 12}}
        log = run([("input", 12), ("change", None), ("input", 14), ("change", None), ("wait", 300)], post=slow)
        self.assertEqual(log[-1]["posts"], [12, 14])

    def test_the_operator_limit_comment_shows_only_when_it_caps_the_chosen_current(self):
        quiet = run([])[0]["warning"]
        capped = run([], status_with(operator_below_set=True, operator_limit_a=reading(11)))[0]["warning"]
        self.assertTrue(quiet["hidden"])
        self.assertFalse(capped["hidden"])
        self.assertEqual(capped["text"], "The AMTRON page's limit of 11 A caps the signalled current.")


if __name__ == "__main__":
    unittest.main()
