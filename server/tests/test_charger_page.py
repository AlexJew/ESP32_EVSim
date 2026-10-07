import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"
SCRIPTS = ("app.js", "chart.js", "dashboard.js")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.tags = []          # (tag, attrs dict), in document order

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "id" in attrs:
            self.ids.add(attrs["id"])


def parsed_page():
    parser = _Page()
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

    def test_the_page_has_the_control_card(self):
        for needed in ("banner", "vehicle-state", "command-status", "installation-limit", "limit-value",
                       "current-limit", "operator-warning", "tick-min", "tick-mid", "tick-max"):
            self.assertIn(needed, page_ids())

    def test_the_page_has_the_chart_and_the_two_lists(self):
        for needed in ("chart", "chart-unit", "chart-note", "legend", "mqtt-state", "connection",
                       "signalled-current", "board-setpoint", "setpoint-value", "setpoints-updated",
                       "meter-charger", "inverter-dc", "inverter-ac", "measurements-updated"):
            self.assertIn(needed, page_ids())

    def test_the_maximum_current_is_a_slider_from_6_amps(self):
        inputs = {attrs["id"]: attrs for tag, attrs in parsed_page().tags if tag == "input" and "id" in attrs}
        slider = inputs["current-limit"]
        self.assertEqual((slider["type"], slider["min"], slider["step"]), ("range", "6", "1"))
        self.assertNotIn("apply", page_ids())                      # it sends on release, there is no Apply button

    def test_the_status_pill_starts_idle_and_live(self):
        pill = [attrs for tag, attrs in parsed_page().tags if attrs.get("id") == "command-status"][0]
        self.assertEqual((pill["data-kind"], pill["role"], pill["aria-live"]), ("idle", "status", "polite"))
        self.assertIn("No command yet", (STATIC / "index.html").read_text(encoding="utf-8"))

    def test_the_vehicle_state_is_a_dropdown_of_a_b_and_c(self):
        select = [attrs for tag, attrs in parsed_page().tags if attrs.get("id") == "vehicle-state"]
        self.assertEqual([attrs.get("class") for attrs in select], ["unit-select"])
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        block = html[html.index('id="vehicle-state"'):html.index("</select>", html.index('id="vehicle-state"'))]
        self.assertEqual(re.findall(r'<option value="([^"]*)"', block), ["", "A", "B", "C"])
        self.assertIn("disabled", block.split(">")[0])                   # off until the board's state is known
        self.assertNotIn("stepper", html)

    def test_both_dropdowns_draw_their_own_arrow_with_room_before_the_edge(self):
        # the browser's native arrow hugs the right edge of the rounded box; the style sheet draws one instead.
        # No image is used: the page's security policy only allows its own files, so the arrow is two CSS gradients.
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        rules = re.findall(r"\.unit-select\s*\{([^}]*)\}", css)
        joined = " ".join(rules)
        self.assertIn("appearance: none", joined)
        self.assertEqual(len(re.findall(r"linear-gradient", joined)), 2)
        self.assertNotIn("url(", joined)
        gap = re.search(r"background-position:\s*calc\(100% - (\d+)px\)[^;]*calc\(100% - (\d+)px\)", joined)
        self.assertIsNotNone(gap)
        self.assertGreaterEqual(min(int(gap.group(1)), int(gap.group(2))) - 6, 12)     # at least 12 px between arrow and edge
        padding = re.search(r"padding-right:\s*(\d+)px", joined)
        self.assertGreaterEqual(int(padding.group(1)), int(gap.group(1)) + 6)         # the text never runs under the arrow
        classes = [attrs.get("class") for tag, attrs in parsed_page().tags if tag == "select"]
        self.assertEqual(classes, ["unit-select", "unit-select"])                     # the state and the chart dropdowns

    def test_the_measurements_card_has_three_toggle_bubbles_a_v_w(self):
        buttons = {attrs["id"]: attrs for tag, attrs in parsed_page().tags if tag == "button" and attrs.get("id", "").startswith("part-")}
        self.assertEqual(sorted(buttons), ["part-A", "part-V", "part-W"])
        for attrs in buttons.values():
            self.assertEqual((attrs["type"], attrs["aria-pressed"]), ("button", "true"))
            self.assertIn("aria-label", attrs)
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('id="measurements-title"'), html.index('id="part-A"'))
        self.assertLess(html.index('id="part-W"'), html.index('id="meter-charger"'))

    def test_the_chart_offers_power_current_and_voltage(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        start = html.index('id="chart-unit"')
        block = html[start:html.index("</select>", start)]
        self.assertEqual(re.findall(r'<option value="([^"]*)">', block), ["W", "A", "V"])

    def test_the_chart_offers_power_and_current(self):
        select = [attrs for tag, attrs in parsed_page().tags if tag == "select" and attrs.get("id") == "chart-unit"]
        self.assertEqual(len(select), 1)
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<option value="W">', html)
        self.assertIn('<option value="A">', html)

    def test_the_removed_cards_are_gone(self):
        for removed in ("operator-limit", "configured-limit", "charger-status", "board-detail", "server-detail",
                        "target-value", "apply", "signalled-updated", "meter-updated", "stepper-value"):
            self.assertNotIn(removed, page_ids())

    def test_no_rest_session_leftovers_in_the_page(self):
        text = script_text("app.js") + (STATIC / "index.html").read_text(encoding="utf-8")
        for leftover in ("reconnect", "session_taken", "not_configured", "Credentials"):
            self.assertNotIn(leftover, text)

    def test_the_scripts_never_build_markup_from_strings(self):
        for name in SCRIPTS:
            text = script_text(name)
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(banned, text, (name, banned))

    def test_the_scripts_load_in_dependency_order(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/chart.js"), html.index("/dashboard.js"))
        self.assertLess(html.index("/dashboard.js"), html.index("/app.js"))       # app.js calls into dashboard.js

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
    def test_the_gap_limit_is_a_parameter_so_the_slower_current_samples_stay_joined(self):
        frames = "[{t:0,v:1},{t:2,v:2},{t:4,v:3}]"
        self.assertEqual(node_json('c.segments(%s, "v", 2.5)' % frames), [[[0, 1], [2, 2], [4, 3]]])
        self.assertEqual(node_json('c.segments(%s, "v", 1.5)' % frames), [[[0, 1]], [[2, 2]], [[4, 3]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_null_value_breaks_the_line_and_a_lone_point_is_kept(self):
        frames = "[{t:0,v:1},{t:1,v:null},{t:2,v:3},{t:3},{t:4,v:5},{t:5,v:6}]"
        self.assertEqual(node_json('c.segments(%s, "v")' % frames), [[[0, 1]], [[2, 3]], [[4, 5], [5, 6]]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_power_axis_steps_with_the_data(self):
        self.assertEqual(
            node_json("[c.axis(0), c.axis(4361), c.axis(12000)]"),
            [{"top": 2000, "step": 1000}, {"top": 6000, "step": 2000}, {"top": 12000, "step": 4000}],
        )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_current_axis_never_goes_below_the_16_amps_the_charger_can_be_set_to(self):
        self.assertEqual(
            node_json("[c.axisAmps(0), c.axisAmps(11.8), c.axisAmps(16), c.axisAmps(17.1)]"),
            [{"top": 16, "step": 4}] * 3 + [{"top": 20, "step": 4}],
        )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_voltage_axis_steps_by_100_volts_and_never_goes_below_300(self):
        self.assertEqual(
            node_json("[c.axisVolts(0), c.axisVolts(233), c.axisVolts(524.7), c.axisVolts(1000)]"),
            [{"top": 300, "step": 100}, {"top": 300, "step": 100}, {"top": 600, "step": 100}, {"top": 1000, "step": 100}],
        )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_voltage_view_names_its_lines_after_the_other_two(self):
        volts = node_json("c.VOLT_SERIES.map((s) => [s.id, s.key])")
        self.assertEqual(volts, [["car", "charger_v"], ["dc", "dc_v"], ["ac", "ac_v"]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_two_views_name_their_lines(self):
        power = node_json("c.POWER_SERIES.map((s) => [s.id, s.key])")
        current = node_json("c.CURRENT_SERIES.map((s) => [s.id, s.key])")
        self.assertEqual(power, [["board", "cp_w"], ["ev", "setpoint_w"], ["car", "charger_w"], ["dc", "dc_w"], ["ac", "ac_w"]])
        self.assertEqual(current, [["sig", "sig_a"], ["car", "charger_a"], ["dc", "dc_a"], ["ac", "ac_a"]])
        shared = {i for i, _ in power} & {i for i, _ in current}
        self.assertEqual(shared, {"car", "dc", "ac"})              # hiding a line hides it in watts and in amps


if __name__ == "__main__":
    unittest.main()
