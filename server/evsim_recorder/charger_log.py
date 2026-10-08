"""Turns each poll of the charger service into a row of the charger table.

The values are the raw ones, per phase (not means). A run of failing polls is one `charger_error` event and its end is
one `charger_ok` event, so a gap in the charger data can be told from a quiet system without one event per second.
"""

import logging

log = logging.getLogger("evsim_recorder.charger_log")

COLUMNS = {                        # charger table column <- register name
    "set_limit_a": "set_limit_a", "operator_limit_a": "operator_limit_a", "installation_limit_a": "installation_limit_a",
    "signalled_current_a": "signalled_current_a", "vehicle_state": "vehicle_state", "charger_status": "charger_status",
    "i_l1_a": "meter_current_l1_a", "i_l2_a": "meter_current_l2_a", "i_l3_a": "meter_current_l3_a",
    "v_l1_v": "meter_voltage_l1_v", "v_l2_v": "meter_voltage_l2_v", "v_l3_v": "meter_voltage_l3_v",
    "power_w": "meter_power_w",
}


class ChargerLogger:
    def __init__(self, store):
        self._store = store
        self._failing = False

    def __call__(self, poll):
        values = poll["values"]
        self._store.add_charger(
            t_start_ns=poll["t_start_ns"], t_end_ns=poll["t_end_ns"], error=poll["error"],
            **{column: values.get(register) for column, register in COLUMNS.items()})
        failing = poll["error"] is not None
        if failing and not self._failing:
            self._store.add_event("charger_error", poll["error"], t_ns=poll["t_end_ns"])
        elif self._failing and not failing:
            self._store.add_event("charger_ok", "polls succeed again", t_ns=poll["t_end_ns"])
        self._failing = failing
