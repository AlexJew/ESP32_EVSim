import importlib
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "charger_web"
TESTS = Path(__file__).resolve().parent


class ModbusOnlyTest(unittest.TestCase):
    def test_the_rest_modules_are_gone(self):
        for name in ("charger", "charger_modbus"):
            with self.assertRaises(ImportError):
                importlib.import_module(f"charger_web.{name}")

    def test_no_rest_session_or_credential_code_remains(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
        for leftover in (
            "urllib.request", "/v1/login", "Authorization", "EVSIM_CHARGER_USERNAME", "EVSIM_CHARGER_PASSWORD",
            "charger_credentials", "SETTINGS_CACHE_S", "DASHBOARD_CACHE_S", "LOGIN_RETRY_DELAY_S",
            "IDLE_RELEASE_S", "HTTP_TIMEOUT_S", "--backend",
        ):
            self.assertNotIn(leftover, text, leftover)

    def test_the_old_rest_and_first_modbus_tests_are_gone(self):
        for name in ("test_charger_turns.py", "test_charger_modbus.py"):
            self.assertFalse((TESTS / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
