---
name: evsim-firmware-layout
description: How the EVSim ESP32 firmware is structured - pure conversion in lib/cp_convert with native tests, ISR edge capture in src/cp_reader, CSV over serial
metadata:
  type: project
---

PlatformIO project in `EVSim/`. Two environments: `esp32dev` (the firmware) and **`native`**
(host-side unit tests).

```
EVSim/lib/cp_convert/    pure duty -> current -> power maths, no Arduino dependency
EVSim/src/cp_reader.*    GPIO35 edge-capture ISR + windowed accumulators
EVSim/src/main.cpp       200 ms sample loop, CSV over serial
EVSim/test/test_cp_convert/  Unity tests, run on the Mac
```

```bash
~/.platformio/penv/bin/pio test -e native     # pio is NOT on PATH
~/.platformio/penv/bin/pio run -e esp32dev
```

**Why the split:** the conversion arithmetic is the part worth testing and the part that has
to be right for the thesis, so it lives in `lib/` where the `native` env can build it
without hardware. The ISR cannot be unit-tested, so it is kept as thin as possible.

Serial output is CSV sharing column names with `tools/charger_poll.py`
([[evsim-charger-poll-tool]]) so the two logs merge on time:
`millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw`.

Invalid states emit **empty** current/power cells - the firmware never invents a value when
the charger is not offering current (see [[evsim-open-questions]] for why that happens every
few seconds).

Design record: `docs/superpowers/specs/2026-09-07-cp-pwm-reader-design.md`.
Measurement uses interrupt timing, not the MCPWM capture unit - revisit only if WiFi
introduces jitter. Related: [[evsim-esp32-pinout]].
