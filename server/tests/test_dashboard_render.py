import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs chart.js and dashboard.js in Node against the stub DOM. The scenario is {"telemetry": ..., "steps": [...]}:
# renderTelemetry runs first, then each step: ["unit", "A"] picks a chart quantity, ["legend", "DC input"] clicks a legend
# entry, ["history", frames] renders a history. The result is what the script put on the page.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { elements, document } = installDom();
global.fetch = () => Promise.reject(new Error("no network in this test"));
const code = ["chart.js", "dashboard.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.page = { renderTelemetry, renderHistory };");
const scenario = JSON.parse(process.argv[2]);
if (scenario.telemetry) page.renderTelemetry(scenario.telemetry);
for (const [kind, a] of scenario.steps || []) {
  if (kind === "unit") document.getElementById("chart-unit").listeners.change({ target: { value: a } });
  if (kind === "legend") document.getElementById("legend").children.find((btn) => btn.children[1].textContent === a).listeners.click();
  if (kind === "history") page.renderHistory({ frames: a });
}
const out = { elements: {}, legend: [] };
for (const [id, el] of Object.entries(elements)) {
  out.elements[id] = { text: el.textContent, status: el.dataset.status || null, state: el.dataset.state || null,
                       title: el.title, value: el.value, disabled: el.disabled === true };
}
for (const btn of document.getElementById("legend").children) {
  out.legend.push({ label: btn.children[1].textContent, pressed: btn.attrs["aria-pressed"], noData: btn.classes.has("no-data") });
}
console.log(JSON.stringify(out));
"""

INVERTER = {"dc_w": 5661.8, "dc_a": 10.9, "dc_v": 520.4, "ac_w": 5500.2, "ac_a": 8.1, "ac_v": 233.3}


def reading(value, status):
    return {"value": value, "status": status, "updated_at": None}


def telemetry(board="ok", setpoint="ok", inverter="ok", connected=True, state="C", values=None):
    def make(value, status):
        return reading(None, "unavailable") if status == "unavailable" else reading(value, status)

    values = INVERTER if values is None else values
    return {
        "mqtt": {"connected": connected},
        "board": {"setpoint_w": make(5739.0, board), "state": state, "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": make(5678.0, setpoint), "status": "ok"},
        "server": {"target_dc_w": make(5682.4, "ok"), "mode": "capped", "inverter_state": 4},
        "inverter": {name: make(values.get(name), inverter) for name in INVERTER},
    }


def run(telemetry_data=None, *steps):
    scenario = {"telemetry": telemetry_data, "steps": list(steps)}
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(scenario), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def labels(out):
    return [entry["label"] for entry in out["legend"]]


def by_label(out, field):
    return {entry["label"]: entry[field] for entry in out["legend"]}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTest(unittest.TestCase):
    def test_the_lists_show_the_setpoints(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["board-setpoint"]["status"]), ("5739 W", "ok"))
        self.assertEqual((out["setpoint-value"]["text"], out["setpoint-value"]["status"]), ("5678 W", "ok"))
        self.assertIn("evsim/setpoint", out["setpoint-value"]["title"])

    def test_the_inverter_rows_show_current_voltage_and_power(self):
        out = run(telemetry())["elements"]
        self.assertEqual((out["inverter-dc"]["text"], out["inverter-dc"]["status"]), ("10.9 A / 520 V / 5662 W", "ok"))
        self.assertEqual((out["inverter-ac"]["text"], out["inverter-ac"]["status"]), ("8.1 A / 233 V / 5500 W", "ok"))
        self.assertTrue(out["measurements-updated"]["text"].startswith("Inverter · "))

    def test_an_old_setpoint_server_leaves_dashes_for_the_new_values(self):
        out = run(telemetry(values={"dc_w": 5661.8, "ac_w": 5500.2}))["elements"]
        self.assertEqual(out["inverter-dc"]["text"], "— / — / 5662 W")
        self.assertEqual(out["inverter-ac"]["text"], "— / — / 5500 W")

    def test_the_broker_badge_follows_the_connection(self):
        up = run(telemetry(connected=True))["elements"]["mqtt-state"]
        down = run(telemetry(connected=False))["elements"]["mqtt-state"]
        self.assertEqual((up["text"], up["state"]), ("Broker connected", "ok"))
        self.assertEqual((down["text"], down["state"]), ("Broker not connected", "unavailable"))

    def test_a_stale_value_is_dimmed_and_its_age_line_says_so(self):
        out = run(telemetry(board="stale", inverter="stale"))["elements"]
        self.assertEqual(out["board-setpoint"]["status"], "stale")
        self.assertTrue(out["setpoints-updated"]["text"].startswith("Stale"))
        self.assertEqual((out["inverter-dc"]["status"], out["inverter-ac"]["status"]), ("stale", "stale"))

    def test_nothing_received_shows_dashes_and_dims_everything(self):
        out = run(telemetry(board="unavailable", setpoint="unavailable", inverter="unavailable"))["elements"]
        self.assertEqual((out["board-setpoint"]["text"], out["setpoint-value"]["text"]), ("—", "—"))
        self.assertEqual((out["inverter-dc"]["text"], out["inverter-ac"]["text"]), ("— / — / —", "— / — / —"))
        for element in ("board-setpoint", "setpoint-value", "inverter-dc", "inverter-ac"):
            self.assertEqual(out[element]["status"], "unavailable", element)
        self.assertEqual(out["setpoints-updated"]["text"], "No reading yet")

    def test_the_power_legend_has_five_entries_all_shown(self):
        out = run()
        self.assertEqual(labels(out), ["Board", "EV setpoint", "Car charger", "DC input", "AC output"])
        self.assertEqual(set(by_label(out, "pressed").values()), {"true"})

    def test_switching_to_amps_and_to_volts_rebuilds_the_legend(self):
        self.assertEqual(labels(run(None, ("unit", "A"))), ["Signalled", "Car charger", "DC input", "AC output"])
        self.assertEqual(labels(run(None, ("unit", "V"))), ["Car charger", "DC input", "AC output"])

    def test_an_unknown_quantity_falls_back_to_power(self):
        self.assertEqual(labels(run(None, ("unit", "X"))), ["Board", "EV setpoint", "Car charger", "DC input", "AC output"])

    def test_a_hidden_line_stays_hidden_across_all_three_views(self):
        out = run(None, ("legend", "DC input"), ("unit", "A"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("unit", "V"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("unit", "A"), ("unit", "W"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "false")
        out = run(None, ("legend", "DC input"), ("legend", "DC input"))
        self.assertEqual(by_label(out, "pressed")["DC input"], "true")

    def test_a_power_line_with_no_data_source_yet_is_marked(self):
        frames = [{"t": 1, "cp_w": 5000, "setpoint_w": 5000, "dc_w": 5000}]
        out = run(None, ("history", frames))
        self.assertEqual(by_label(out, "noData"),
                         {"Board": False, "EV setpoint": False, "Car charger": True, "DC input": False, "AC output": True})

    def test_all_three_charts_draw_the_servers_history(self):
        frames = [{"t": 1, "sig_a": 12.0, "charger_a": 11.8, "dc_a": 10.9, "ac_a": 8.1,
                   "charger_w": 8245, "dc_w": 5662, "ac_w": 5500, "cp_w": 5700, "setpoint_w": 5700,
                   "charger_v": 232, "dc_v": 520, "ac_v": 233}]
        for unit in ("W", "A", "V"):
            out = run(None, ("history", frames), ("unit", unit))
            self.assertEqual(set(by_label(out, "noData").values()), {False}, unit)

    def test_the_current_chart_is_empty_until_the_history_carries_currents(self):
        frames = [{"t": 1, "cp_w": 5000, "setpoint_w": 5000, "dc_w": 5000}]
        out = run(None, ("history", frames), ("unit", "A"))
        self.assertEqual(set(by_label(out, "noData").values()), {True})

    def test_the_dropdown_follows_the_board_and_is_off_when_the_state_is_unknown(self):
        for state, value, disabled in (("C", "C", False), ("A", "A", False), (None, "", True), ("X", "", True)):
            select = run(telemetry(state=state))["elements"]["vehicle-state"]
            self.assertEqual((select["value"], select["disabled"]), (value, disabled), state)

    def test_the_dropdown_is_off_while_the_broker_is_down(self):
        select = run(telemetry(connected=False, state="C"))["elements"]["vehicle-state"]
        self.assertEqual((select["value"], select["disabled"]), ("", True))


if __name__ == "__main__":
    unittest.main()
