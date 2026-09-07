---
name: evsim-charger-poll-tool
description: tools/charger_poll.py - stdlib-only script that logs the AMTRON's signaled CP current to CSV for calibrating the ESP32's PWM reading
metadata:
  type: project
---

`tools/charger_poll.py` in the repo root (`ESP32_EVSim/tools/`). Standard library only - no pip install.

Polls `/json/dashboard.json`, extracts `SignaledCurrentLimit_vehicleif`, and computes the **expected CP
duty cycle** from it via the gonium formula, so the output can be lined up directly against what the ESP32
measures. See [[evsim-amtron-live-telemetry]] and [[evsim-reference-gonium]].

```
export EVSIM_TOKEN=<uuid from a browser request>
python3 tools/charger_poll.py --duration 120 --interval 1 --csv run1.csv
python3 tools/charger_poll.py --sweep 6,8,10,13,16 --dwell 20 --csv sweep.csv   # WRITES
```

CSV columns: `iso_time, elapsed_s, operator_limit_a, signaled_a, expected_duty_pct, offering,
type2_state, ocpp_state, meter_current`. The `offering` flag is 1 during on-windows, 0 during the
no-load fault windows - **filter on it when calibrating** (see [[evsim-open-questions]]).

`--sweep` steps the operator limit and dwells at each value; it **writes to the charger** and refuses
steps above `InstallationCurrentLimit_vehicleif`. Plain polling is read-only.

The token is never stored in the repo - it comes from `$EVSIM_TOKEN` or `--token`. A 401 means it expired;
fetch a fresh one from the browser. `*.csv` is gitignored.
