import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "charger_web" / "static"

# Runs chart.js and dashboard.js in Node against the stub DOM, fires the chart's events in order and logs the zoom state
# after each. The clock is fixed at 1000 s, so the whole window is [400, 1000]. The plot is 730 px wide (800 px canvas,
# 58 px left margin, 12 px right margin), so the middle of the plot is clientX = 423.
HARNESS = r"""
const fs = require("fs");
const { installDom } = require(process.argv[3]);
const { document } = installDom();
global.fetch = () => Promise.reject(new Error("no network in this test"));
Date.now = () => 1000 * 1000;
const code = ["chart.js", "dashboard.js"].map((name) => fs.readFileSync(process.argv[1] + "/" + name, "utf8")).join("\n");
eval(code + "\nglobalThis.state = dashboardState; globalThis.renderHistory = renderHistory;");
let prevented = 0;
const fire = (id, type, event) => {
  const handler = document.getElementById(id).listeners[type];
  handler(Object.assign({ preventDefault() { prevented += 1; }, deltaMode: 0, ctrlKey: false, pointerId: 1 }, event));
};
const log = [];
for (const [id, type, event] of JSON.parse(process.argv[2])) {
  if (id === "history") renderHistory({ frames: [] });
  else fire(id, type, event || {});
  log.push({
    zoom: state.zoom, prevented,
    liveHidden: document.getElementById("chart-live").hidden,
    viewHidden: document.getElementById("chart-view").hidden,
    viewText: document.getElementById("chart-view").textContent,
  });
}
console.log(JSON.stringify(log));
"""

# Draws the chart on a recording canvas and reports what was drawn: the texts, how many clips and dots.
DRAW = r"""
const fs = require("fs");
global.window = { devicePixelRatio: 1 };
const calls = [];
const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : (...a) => { calls.push([k, ...a]); }), set: (t, k, v) => (t[k] = v, true) });
const canvas = { clientWidth: 800, clientHeight: 300, getContext: () => ctx };
eval(fs.readFileSync(process.argv[1] + "/chart.js", "utf8") + "\nglobalThis.D = { drawChart, POWER_VIEW, POWER_SERIES, CURRENT_VIEW: { series: CURRENT_SERIES, unit: 'A', maxGapS: MAX_GAP_S } };");
const spec = JSON.parse(process.argv[2]);
const now = 1000;
const frames = [];
for (let i = 1; i <= 600; i++) frames.push({ t: now - 600 + i, setpoint_w: spec.early && i < 300 ? 9000 : 4000 + (i % 7), dc_w: 4100, sig_a: 11 });
const visible = new Set(D.POWER_SERIES.map((s) => s.id).concat(["sig"]));
D.drawChart(canvas, frames, visible, now, spec.view === "A" ? D.CURRENT_VIEW : D.POWER_VIEW, spec.zoom);
const texts = calls.filter((c) => c[0] === "fillText").map((c) => c[1]);
console.log(JSON.stringify({ texts, clips: calls.filter((c) => c[0] === "clip").length, dots: calls.filter((c) => c[0] === "arc").length,
                             lines: calls.filter((c) => c[0] === "lineTo").length }));
"""

MIDDLE = 423


def run(*steps):
    result = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(steps), str(HERE / "dom_stub.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def draw(zoom=None, view="W", early=False):
    spec = {"zoom": zoom, "view": view, "early": early}
    result = subprocess.run(["node", "-e", DRAW, str(STATIC), json.dumps(spec)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def wheel(delta, x=MIDDLE, **extra):
    return ("chart", "wheel", dict({"deltaY": delta, "clientX": x}, **extra))


def span(entry):
    return entry["zoom"]["to"] - entry["zoom"]["from"]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ZoomTest(unittest.TestCase):
    def test_the_page_starts_following_live_with_no_zoom_controls_showing(self):
        log = run(("history", "", {}))
        self.assertIsNone(log[0]["zoom"])
        self.assertTrue(log[0]["liveHidden"])
        self.assertTrue(log[0]["viewHidden"])

    def test_the_wheel_zooms_in_around_the_cursor_and_shows_the_button(self):
        entry = run(wheel(-300))[0]
        self.assertLess(span(entry), 600)
        self.assertAlmostEqual(entry["zoom"]["from"] + span(entry) / 2, 700, places=3)       # the middle stays at 700 s
        self.assertFalse(entry["liveHidden"])
        self.assertFalse(entry["viewHidden"])
        self.assertEqual(entry["viewText"], "Showing %d s" % round(span(entry)))

    def test_the_wheel_stops_the_page_from_scrolling(self):
        self.assertEqual(run(wheel(-300))[0]["prevented"], 1)

    def test_a_pinch_zooms_too_and_a_line_wheel_counts_as_lines(self):
        self.assertLess(span(run(wheel(-5, ctrlKey=True))[0]), 600)
        self.assertLess(span(run(wheel(-3, deltaMode=1))[0]), 600)

    def test_zooming_out_past_the_whole_window_follows_live_again(self):
        log = run(wheel(-300), wheel(2000), wheel(2000))
        self.assertIsNotNone(log[0]["zoom"])
        self.assertIsNone(log[2]["zoom"])
        self.assertTrue(log[2]["liveHidden"])
        self.assertTrue(log[2]["viewHidden"])

    def test_double_click_and_the_button_go_back_to_live(self):
        for step in (("chart", "dblclick", {}), ("chart-live", "click", {})):
            log = run(wheel(-300), step)
            self.assertIsNone(log[1]["zoom"])
            self.assertTrue(log[1]["liveHidden"])
            self.assertEqual(log[1]["viewText"], "")

    def test_dragging_a_zoomed_chart_pans_it(self):
        log = run(wheel(-300), ("chart", "pointerdown", {"clientX": 400}), ("chart", "pointermove", {"clientX": 500}),
                  ("chart", "pointerup", {}), ("chart", "pointermove", {"clientX": 700}))
        zoomed, panned, released = log[0]["zoom"], log[2]["zoom"], log[4]["zoom"]
        per_pixel = (zoomed["to"] - zoomed["from"]) / 730
        self.assertAlmostEqual(panned["from"], zoomed["from"] - 100 * per_pixel, places=3)      # dragging right shows earlier time
        self.assertAlmostEqual(panned["to"] - panned["from"], zoomed["to"] - zoomed["from"], places=3)
        self.assertEqual(released, panned)                                                       # a move after the release is ignored

    def test_dragging_the_whole_window_does_nothing(self):
        log = run(("chart", "pointerdown", {"clientX": 400}), ("chart", "pointermove", {"clientX": 500}))
        self.assertIsNone(log[1]["zoom"])

    def test_a_history_refresh_keeps_the_zoom(self):
        log = run(wheel(-300), ("history", "", {}))
        self.assertEqual(log[1]["zoom"], log[0]["zoom"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DrawTest(unittest.TestCase):
    """The chart that is not zoomed must look exactly as it did before the zoom was added."""

    def test_the_live_chart_keeps_its_three_relative_labels_and_no_clip_or_dots(self):
        out = draw()
        self.assertEqual([t for t in out["texts"] if not t.endswith(" W")], ["−10 min", "−5 min", "now"])
        self.assertEqual((out["clips"], out["dots"]), (0, 0))

    def test_the_live_chart_scales_to_the_largest_value_of_the_whole_window(self):
        out = draw(early=True)                                      # 9000 W early in the window
        self.assertIn("10000 W", out["texts"])

    def test_zoomed_in_the_axis_shows_clock_times_and_the_drawing_is_clipped(self):
        out = draw(zoom={"from": 900, "to": 940})
        labels = [t for t in out["texts"] if not t.endswith(" W")]
        self.assertTrue(labels)
        for label in labels:
            self.assertRegex(label, r"^\d\d:\d\d:\d\d$")
        self.assertEqual(out["clips"], 1)

    def test_zoomed_out_a_little_the_axis_shows_hours_and_minutes(self):
        labels = [t for t in draw(zoom={"from": 500, "to": 1000})["texts"] if not t.endswith(" W")]
        for label in labels:
            self.assertRegex(label, r"^\d\d:\d\d$")

    def test_zoomed_in_every_frame_gets_a_marker_and_zoomed_out_none_do(self):
        self.assertGreater(draw(zoom={"from": 900, "to": 940})["dots"], 40)
        self.assertEqual(draw(zoom={"from": 400, "to": 1000 - 1})["dots"], 0)

    def test_the_vertical_scale_follows_what_is_in_view_when_zoomed(self):
        out = draw(zoom={"from": 900, "to": 940}, early=True)       # the 9000 W stretch is outside the view
        self.assertNotIn("10000 W", out["texts"])
        self.assertIn("6000 W", out["texts"])

    def test_the_current_chart_zooms_too(self):
        out = draw(zoom={"from": 900, "to": 940}, view="A")
        self.assertTrue(any(t.endswith(" A") for t in out["texts"]))
        self.assertEqual(out["clips"], 1)


if __name__ == "__main__":
    unittest.main()
