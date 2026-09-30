"""Tests for the Fronius Modbus client and command line, against a fake inverter on localhost.

Nothing here touches the real inverter.
"""

import contextlib
import io
import unittest

from fake_inverter import FakeInverter, M1, M103, M123, M160
from setpoint_server import fronius as fm
from setpoint_server import fronius_cli as cli


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInverter()
        self.port = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.modbus = fm.ModbusTcp("127.0.0.1", self.port)
        self.addCleanup(self.modbus.close)
        self.inv = fm.Inverter(self.modbus)


class PureFunctionsTest(unittest.TestCase):
    def test_pct_register_matches_the_values_used_on_the_bench(self):
        self.assertEqual(fm.pct_register(7050, 12000, -2), 5875)
        self.assertEqual(fm.pct_register(6000, 12000, -2), 5000)
        self.assertEqual(fm.pct_register(0, 12000, -2), 0)
        self.assertEqual(fm.pct_register(12000, 12000, -2), 10000)

    def test_pct_register_rejects_out_of_range(self):
        for watts in (-1, 12001):
            with self.assertRaises(ValueError):
                fm.pct_register(watts, 12000, -2)

    def test_ac_cap_for_dc_uses_the_calibration(self):
        # DC = 1.0303 * AC + 6.3, plus 3 W for the settling offset
        self.assertAlmostEqual(fm.ac_cap_for_dc(4000), (4000 - 6.3) / 1.0303 + 3.0, places=6)
        self.assertEqual(fm.pct_register(fm.ac_cap_for_dc(4000), 12000, -2), 3233)
        self.assertEqual(fm.ac_cap_for_dc(0), 0.0)

    def test_scaled_and_signed(self):
        self.assertEqual(fm.s16(65534), -2)
        self.assertAlmostEqual(fm.scaled(14100, 65533), 14.1)


class ModbusTest(Base):
    def test_out_of_range_read_is_a_clear_error(self):
        with self.assertRaises(IOError) as cm:
            self.modbus.read(100, 2)
        self.assertIn("exception 2", str(cm.exception))

    def test_a_refused_write_is_a_clear_error(self):
        self.fake.refuse_writes = True
        with self.assertRaises(IOError) as cm:
            self.modbus.write_single(M123 + 7, 1)
        self.assertIn("exception 3", str(cm.exception))


class ReadTest(Base):
    def test_discovers_the_model_chain(self):
        models = self.inv.models()
        self.assertEqual(models[1], (M1, 65))
        self.assertEqual(models[103], (M103, 50))
        self.assertEqual(models[123], (M123, 24))
        self.assertEqual(models[160], (M160, 88))
        self.assertIn(124, models)

    def test_identity_and_rating(self):
        ident = self.inv.identity()
        self.assertEqual(ident["manufacturer"], "Fronius")
        self.assertEqual(ident["model"], "Symo GEN24 12.0 Plus SC")
        self.assertEqual(ident["version"], "1.41.11-1")
        self.assertEqual(self.inv.wmax(), 12000)

    def test_controls_are_decoded(self):
        c = self.inv.controls()
        self.assertEqual((c["conn"], c["pct_raw"], c["ena"], c["sf"]), (1, 10000, 0, -2))
        self.assertAlmostEqual(c["pct"], 100.0)

    def test_power_reads_ac_and_both_mppt_inputs(self):
        p = self.inv.power()
        self.assertEqual(p["ac_w"], 7047)
        self.assertEqual(p["state"], 4)
        m1, m2 = p["mppt"][0], p["mppt"][1]
        self.assertEqual((m1["name"], m1["dc_w"]), ("MPPT 1", 7272))
        self.assertAlmostEqual(m1["dc_a"], 14.1)
        self.assertAlmostEqual(m1["dc_v"], 515.0)
        self.assertIsNone(m2["dc_w"])   # 65535 = not implemented

    def test_int16_not_implemented_is_none(self):
        self.fake.put(M103 + 12, [0x8000])
        self.assertIsNone(self.inv.power()["ac_w"])

    def test_scale_factors_are_re_read_every_time(self):
        self.assertEqual(self.inv.power()["mppt"][0]["dc_w"], 7272)
        # the inverter changes the DC power scale factor at run time (seen at start-up)
        self.fake.put(M160 + 2, [-1])
        self.fake.put(M160 + 8 + 11, [65348])
        self.assertAlmostEqual(self.inv.power()["mppt"][0]["dc_w"], 6534.8)

    def test_reading_never_writes(self):
        self.inv.identity(), self.inv.controls(), self.inv.power(), self.inv.wmax()
        self.assertEqual(self.fake.writes, [])

    def test_missing_model_123_is_reported(self):
        fake = FakeInverter(with_123=False)
        port = fake.start()
        self.addCleanup(fake.stop)
        modbus = fm.ModbusTcp("127.0.0.1", port)
        self.addCleanup(modbus.close)
        with self.assertRaises(IOError) as cm:
            fm.Inverter(modbus).controls()
        self.assertIn("123", str(cm.exception))


class WriteTest(Base):
    def test_set_writes_the_value_then_commits_with_ena(self):
        self.inv.set_ac_cap(6000)
        self.assertEqual(self.fake.writes, [(M123 + 3, 5000), (M123 + 7, 1)])

    def test_set_reads_back_and_reports(self):
        c = self.inv.set_ac_cap(7050)
        self.assertEqual((c["pct_raw"], c["ena"]), (5875, 1))

    def test_set_zero_is_allowed_and_commits(self):
        self.inv.set_ac_cap(0)
        self.assertEqual(self.fake.writes, [(M123 + 3, 0), (M123 + 7, 1)])

    def test_set_out_of_range_writes_nothing(self):
        for watts in (-5, 12500):
            with self.assertRaises(ValueError):
                self.inv.set_ac_cap(watts)
        self.assertEqual(self.fake.writes, [])

    def test_set_dc_converts_through_the_calibration(self):
        self.inv.set_dc_target(4000)
        self.assertEqual(self.fake.writes, [(M123 + 3, 3233), (M123 + 7, 1)])

    def test_off_disables_then_restores_100_percent(self):
        self.inv.clear_cap()
        self.assertEqual(self.fake.writes, [(M123 + 7, 0), (M123 + 3, 10000)])

    def test_conn_takes_only_zero_or_one(self):
        self.inv.set_conn(0)
        self.inv.set_conn(1)
        self.assertEqual(self.fake.writes, [(M123 + 2, 0), (M123 + 2, 1)])
        for bad in (2, -1):
            with self.assertRaises(ValueError):
                self.inv.set_conn(bad)

    def test_a_conn_write_that_is_not_kept_is_an_error(self):
        real = self.modbus.write_single

        def lossy(addr, value):
            real(addr, value)
            self.fake.regs[addr] = 1 - value

        self.modbus.write_single = lossy
        with self.assertRaises(IOError) as cm:
            self.inv.set_conn(0)
        self.assertIn("read back", str(cm.exception))

    def test_no_other_register_can_be_written(self):
        with self.assertRaises(ValueError):
            self.inv._write("wmax", 1)
        self.assertEqual(self.fake.writes, [])

    def test_a_refused_write_is_not_reported_as_success(self):
        self.fake.refuse_writes = True
        with self.assertRaises(IOError):
            self.inv.set_ac_cap(6000)

    def test_a_readback_mismatch_is_an_error(self):
        # the inverter accepts the write but does not keep the value
        real = self.modbus.write_single

        def lossy(addr, value):
            real(addr, value)
            self.fake.regs[addr] = 1234

        self.modbus.write_single = lossy
        with self.assertRaises(IOError) as cm:
            self.inv.set_ac_cap(6000)
        self.assertIn("read back", str(cm.exception))


class CliTest(Base):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--host", "127.0.0.1", "--port", str(self.port)] + list(args))
        return code, out.getvalue()

    def test_status_prints_one_line_and_writes_nothing(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Ena=0", out)
        self.assertIn("AC 7047 W", out)
        self.assertEqual(self.fake.writes, [])

    def test_status_shows_only_the_mppt_inputs(self):
        code, out = self.run_cli("status")
        self.assertIn("MPPT 1 7272 W", out)
        self.assertNotIn("StCha", out)

    def test_probe_is_read_only_and_names_the_unit(self):
        code, out = self.run_cli("probe")
        self.assertEqual(code, 0)
        self.assertIn("Symo GEN24 12.0 Plus SC", out)
        self.assertIn("model 123", out)
        self.assertEqual(self.fake.writes, [])

    def test_set_commits(self):
        code, out = self.run_cli("set", "6000")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.writes, [(M123 + 3, 5000), (M123 + 7, 1)])

    def test_set_out_of_range_is_an_error_and_writes_nothing(self):
        code, out = self.run_cli("set", "13000")
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertEqual(self.fake.writes, [])

    def test_unreachable_inverter_is_an_error_not_a_traceback(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["--host", "127.0.0.1", "--port", "1", "--timeout", "1", "status"])
        self.assertEqual(code, 1)
        self.assertIn("error", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())


class CloseTest(Base):
    def test_close_releases_the_connection(self):
        self.inv.close()
        with self.assertRaises(OSError):
            self.inv.controls()


if __name__ == "__main__":
    unittest.main()
