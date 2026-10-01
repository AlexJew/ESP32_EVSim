import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "charger_web" / "static"


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        self.ids.update(value for key, value in attrs if key == "id")


def page_ids():
    parser = _Ids()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser.ids


class PageTest(unittest.TestCase):
    def test_every_element_the_script_looks_up_exists_in_the_page(self):
        used = set(re.findall(r'\bel\("([^"]+)"\)', (STATIC / "app.js").read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(sorted(used - page_ids()), [])

    def test_the_page_has_the_modbus_elements(self):
        for needed in ("banner", "operator-limit", "operator-warning", "vehicle-state", "charger-status"):
            self.assertIn(needed, page_ids())

    def test_no_rest_session_leftovers_in_the_page(self):
        text = (STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "index.html").read_text(encoding="utf-8")
        for leftover in ("reconnect", "session_taken", "not_configured", "Credentials"):
            self.assertNotIn(leftover, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_parses(self):
        result = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
