import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"

# Runs chart.js and dashboard.js in Node against a stub DOM and prints what renderTelemetry put on each element.
HARNESS = r"""
const fs = require("fs");
const elements = {};
const make = (id) => ({
  id, textContent: "", dataset: {}, clientWidth: 800, clientHeight: 300,
  setAttribute() {}, addEventListener() {},
  getContext: () => new Proxy({}, { get: () => () => {}, set: () => true }),
});
global.document = { getElementById: (id) => (elements[id] = elements[id] || make(id)), querySelectorAll: () => [] };
global.window = { devicePixelRatio: 1, addEventListener() {}, setInterval() {} };
global.fetch = () => Promise.reject(new Error("no network in this test"));
const code = ["chart.js", "dashboard.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.renderTelemetry = renderTelemetry;");
renderTelemetry(JSON.parse(process.argv[2]));
const out = {};
for (const [id, el] of Object.entries(elements)) out[id] = { text: el.textContent, status: el.dataset.status || null };
console.log(JSON.stringify(out));
"""


def reading(value, status):
    return {"value": value, "status": status, "updated_at": None}


def telemetry(board="ok", setpoint="ok", target="ok", dc="ok"):
    def make(value, status):
        return reading(None, "unavailable") if status == "unavailable" else reading(value, status)

    return {
        "mqtt": {"connected": True},
        "board": {"setpoint_w": make(5739.0, board), "state": "C", "status": "live"},
        "setpoint": {"topic": "evsim/setpoint", "setpoint_w": make(5678.0, setpoint), "status": "ok"},
        "server": {"target_dc_w": make(5682.4, target), "mode": "capped", "inverter_state": 4},
        "inverter": {"dc_w": make(5661.8, dc)},
    }


def render(data):
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(data)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTest(unittest.TestCase):
    def test_healthy_cards_show_their_values_at_full_strength(self):
        out = render(telemetry())
        self.assertEqual((out["board-setpoint"]["text"], out["board-setpoint"]["status"]), ("5739 W", "ok"))
        self.assertEqual((out["board-detail"]["text"], out["board-detail"]["status"]), ("live · pilot C", "ok"))
        self.assertEqual((out["inverter-dc"]["text"], out["target-value"]["text"]), ("5662 W", "5682 W"))

    def test_a_stale_board_dims_the_words_as_well_as_the_number(self):
        out = render(telemetry(board="stale"))
        self.assertEqual(out["board-setpoint"]["status"], "stale")
        self.assertEqual(out["board-detail"]["status"], "stale")          # "live · pilot C" must not look current

    def test_a_stale_setpoint_dims_the_server_line(self):
        out = render(telemetry(setpoint="stale"))
        self.assertEqual(out["setpoint-value"]["status"], "stale")
        self.assertEqual(out["server-detail"]["status"], "stale")

    def test_nothing_received_shows_dashes_and_dims_everything(self):
        out = render(telemetry(board="unavailable", setpoint="unavailable", target="unavailable", dc="unavailable"))
        self.assertEqual((out["board-setpoint"]["text"], out["inverter-dc"]["text"]), ("—", "—"))
        for element in ("board-setpoint", "board-detail", "setpoint-value", "server-detail", "target-value", "inverter-dc"):
            self.assertEqual(out[element]["status"], "unavailable", element)


if __name__ == "__main__":
    unittest.main()
