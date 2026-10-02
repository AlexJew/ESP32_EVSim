import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"
SCRIPTS = ("app.js", "chart.js", "dashboard.js")
SERIES = {"cp_w", "setpoint_w", "target_dc_w", "dc_w"}


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.series = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.add(value)
            if key == "data-series":
                self.series.append(value)


def parsed_page():
    parser = _Ids()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def page_ids():
    return parsed_page().ids


def script_text(name):
    return (STATIC / name).read_text(encoding="utf-8")


def node_json(expression):
    script = "const c = require(%s); console.log(JSON.stringify(%s));" % (json.dumps(str(STATIC / "chart.js")), expression)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class PageTest(unittest.TestCase):
    def test_every_element_the_scripts_look_up_exists_in_the_page(self):
        used = set()
        for name in SCRIPTS:
            used |= set(re.findall(r'\b(?:el|byId)\("([^"]+)"\)', script_text(name)))
        self.assertTrue(used)
        self.assertEqual(sorted(used - page_ids()), [])

    def test_the_page_has_the_modbus_elements(self):
        for needed in ("banner", "operator-limit", "operator-warning", "vehicle-state", "charger-status"):
            self.assertIn(needed, page_ids())

    def test_the_page_has_the_dashboard_elements(self):
        for needed in ("mqtt-state", "board-setpoint", "board-detail", "board-updated", "setpoint-label",
                       "setpoint-value", "target-value", "server-detail", "setpoint-updated", "inverter-dc",
                       "inverter-updated", "chart", "chart-note", "legend-setpoint"):
            self.assertIn(needed, page_ids())

    def test_no_rest_session_leftovers_in_the_page(self):
        text = script_text("app.js") + (STATIC / "index.html").read_text(encoding="utf-8")
        for leftover in ("reconnect", "session_taken", "not_configured", "Credentials"):
            self.assertNotIn(leftover, text)

    def test_the_scripts_never_build_markup_from_strings(self):
        for name in SCRIPTS:
            text = script_text(name)
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(banned, text, (name, banned))

    def test_the_legend_has_one_entry_per_series_and_a_canvas(self):
        page = parsed_page()
        self.assertEqual(sorted(page.series), sorted(SERIES))
        self.assertIn("chart", page.ids)

    def test_the_scripts_load_in_dependency_order(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/chart.js"), html.index("/dashboard.js"))
        self.assertIn("/app.js", html)

    def test_the_heading_is_evsim(self):
        self.assertIn("<h1>EVSim</h1>", (STATIC / "index.html").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_scripts_parse(self):
        for name in SCRIPTS:
            result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, (name, result.stderr))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_missing_frame_breaks_the_line(self):
        frames = "[{t:0,v:1},{t:1,v:2},{t:2,v:3},{t:6,v:4},{t:7,v:5}]"
        self.assertEqual(node_json('c.segments(%s, "v")' % frames), [[[0, 1], [1, 2], [2, 3]], [[6, 4], [7, 5]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_null_value_breaks_the_line_and_a_lone_point_is_kept(self):
        frames = "[{t:0,v:1},{t:1,v:null},{t:2,v:3},{t:3},{t:4,v:5},{t:5,v:6}]"
        self.assertEqual(node_json('c.segments(%s, "v")' % frames), [[[0, 1]], [[2, 3]], [[4, 5], [5, 6]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_axis_steps_with_the_data(self):
        self.assertEqual(
            node_json("[c.axis(0), c.axis(4361), c.axis(12000)]"),
            [{"top": 2000, "step": 1000}, {"top": 6000, "step": 2000}, {"top": 12000, "step": 4000}],
        )


if __name__ == "__main__":
    unittest.main()
