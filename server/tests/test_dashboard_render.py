import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs chart.js and dashboard.js in Node against the stub DOM. The scenario is {"telemetry": ..., "steps": [...]}:
# renderTelemetry runs first, then each step: ["unit", "A"] picks a chart quantity, ["legend", "DC input"] clicks a legend
# entry, ["sample", signalledReading, chargerA] records a current sample. The result is what the script put on the page.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { elements, document } = installDom();
global.fetch = () => Promise.reject(new Error("no network in this test"));
const code = ["chart.js", "dashboard.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.page = { renderTelemetry, recordCurrentSample, renderHistory };");
const scenario = JSON.parse(process.argv[2]);
if (scenario.telemetry) page.renderTelemetry(scenario.telemetry);
for (const [kind, a, b] of scenario.steps || []) {
  if (kind === "unit") document.getElementById("chart-unit").listeners.change({ target: { value: a } });
  if (kind === "legend") document.getElementById("legend").children.find((btn) => btn.children[1].textContent === a).listeners.click();
  if (kind === "sample") page.recordCurrentSample(a, b);
  if (kind === "history") page.renderHistory({ frames: a });
}
const out = { elements: {}, legend: [] };
for (const [id, el] of Object.entries(elements)) out.elements[id] = { text: el.textContent, status: el.dataset.status || null, state: el.dataset.state || null, title: el.title };
for (const btn of document.getElementById("legend").children) {
  out.legend.push({ label: btn.children[1].textContent, pressed: btn.attrs["aria-pressed"], noData: btn.classes.has("no-data") });
}
console.log(JSON.stringify(out));
"""


def reading(value, status):
    return {"value": value, "status": status, "updated_at": None}


def telemetry(board="ok", setpoint="ok", dc="ok", connected=True):
    def make(value, status):
        return reading(None, "unavailable") if status == "unavailable" else reading(value, status)

    return {
        "mqtt": {"connected": connected},
        "board": {"setpoint_w": make(5739.0, board), "state": "C", "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": make(5678.0, setpoint), "status": "ok"},
        "server": {"target_dc_w": make(5682.4, "ok"), "mode": "capped", "inverter_state": 4},
        "inverter": {"dc_w": make(5661.8, dc)},
    }


def run(telemetry_data=None, *steps):
    scenario = {"telemetry": telemetry_data, "steps": list(steps)}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def labels(out):
    return [entry["label"] for entry in out["legend"]]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTest(unittest.TestCase):
    def test_the_lists_show_the_setpoints_and_the_inverter_dc_power(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["board-setpoint"]["status"]), ("5739 W", "ok"))
        self.assertEqual((out["setpoint-value"]["text"], out["setpoint-value"]["status"]), ("5678 W", "ok"))
        self.assertEqual((out["inverter-dc"]["text"], out["inverter-dc"]["status"]), ("— / 5662 W", "ok"))
        self.assertIn("evsim/setpoint", out["setpoint-value"]["title"])
        self.assertTrue(out["measurements-updated"]["text"].startswith("Inverter · "))

    def test_the_ac_side_is_a_dash_until_it_is_read(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["inverter-ac"]["text"], out["inverter-ac"]["status"]), ("— / —", "unavailable"))

    def test_the_broker_badge_follows_the_connection(self):
        up = run(telemetry(connected=True))["elements"]["mqtt-state"]
        down = run(telemetry(connected=False))["elements"]["mqtt-state"]
        self.assertEqual((up["text"], up["state"]), ("Broker connected", "ok"))
        self.assertEqual((down["text"], down["state"]), ("Broker not connected", "unavailable"))

    def test_a_stale_value_is_dimmed_and_its_age_line_says_so(self):
        out = run(telemetry(board="stale", dc="stale"))["elements"]
        self.assertEqual(out["board-setpoint"]["status"], "stale")
        self.assertTrue(out["setpoints-updated"]["text"].startswith("Stale"))
        self.assertEqual(out["inverter-dc"]["status"], "stale")

    def test_nothing_received_shows_dashes_and_dims_everything(self):
        out = run(telemetry(board="unavailable", setpoint="unavailable", dc="unavailable"))["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["setpoint-value"]["text"], out["inverter-dc"]["text"]),
                         ("—", "—", "— / —"))
        for element in ("board-setpoint", "setpoint-value", "inverter-dc"):
            self.assertEqual(out[element]["status"], "unavailable", element)
        self.assertEqual(out["setpoints-updated"]["text"], "No reading yet")

    def test_the_power_legend_has_five_entries_all_shown(self):
        out = run()
        self.assertEqual(labels(out), ["Board", "EV setpoint", "Car charger", "DC input", "AC output"])
        self.assertEqual({entry["pressed"] for entry in out["legend"]}, {"true"})

    def test_switching_to_amps_rebuilds_the_legend(self):
        out = run(None, ("unit", "A"))
        self.assertEqual(labels(out), ["Signalled", "Car charger", "DC input", "AC output"])

    def test_a_hidden_line_stays_hidden_across_the_switch_and_back(self):
        out = run(None, ("legend", "DC input"), ("unit", "A"))
        self.assertEqual({e["label"]: e["pressed"] for e in out["legend"]}["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("unit", "A"), ("unit", "W"))
        self.assertEqual({e["label"]: e["pressed"] for e in out["legend"]}["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("legend", "DC input"))
        self.assertEqual({e["label"]: e["pressed"] for e in out["legend"]}["DC input"], "true")

    def test_a_line_with_no_data_source_yet_is_marked(self):
        frames = [{"t": 1, "cp_w": 5000, "setpoint_w": 5000, "dc_w": 5000}]
        out = run(None, ("history", frames))
        no_data = {e["label"]: e["noData"] for e in out["legend"]}
        self.assertEqual(no_data, {"Board": False, "EV setpoint": False, "Car charger": True, "DC input": False, "AC output": True})

    def test_the_current_chart_keeps_its_own_samples_from_the_charger_polls(self):
        out = run(None, ("unit", "A"), ("sample", reading(11, "ok"), 11.8))
        no_data = {e["label"]: e["noData"] for e in out["legend"]}
        self.assertEqual(no_data, {"Signalled": False, "Car charger": False, "DC input": True, "AC output": True})

    def test_a_stale_signalled_current_is_not_recorded_as_a_value(self):
        out = run(None, ("unit", "A"), ("sample", reading(11, "stale"), None))
        self.assertTrue({e["label"]: e["noData"] for e in out["legend"]}["Signalled"])


if __name__ == "__main__":
    unittest.main()
