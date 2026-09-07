---
name: evsim-esp32-pinout
description: EVSim ESP32-DevkitC pin assignment - GPIO35 reads CP PWM, GPIO33 drives state B (2k7), GPIO32 drives state C (1k2), with a B-before-C interlock
metadata:
  type: project
---

Board: **ESP32-DevkitC**. PlatformIO project lives in `EVSim/` (env `esp32dev`). Same mapping on the
current protoboard and on the PCB being fabricated, so firmware does not need to change when it arrives.

| Function | Leg | MOSFET | Drive pin |
|---|---|---|---|
| S1 - state **B** | 2.7 kOhm | Q1 | **GPIO33** |
| S2 - state **C** | 1.2 kOhm | Q2 | **GPIO32** |
| CP PWM reader | 47k/56k divider | - | **GPIO35** (ADC1, input-only) |

**Interlock:** GPIO32 (state C) may only be asserted **while GPIO33 (state B) is already asserted** -
state C is the 1k2 in parallel with the 2k7, not a replacement for it. Firmware must enforce
A -> B -> C ordering and never drive C alone.

**GPIO34 is NOT usable** - it appears as a `VDET_1` stub in the schematic but was left
unconnected on the fabricated PCB. Anything needing the CP level (including ADC sampling)
must use GPIO35.

Front end between CP and GPIO35 (schematic rev 1.0): `D1` 1N4007 in series strips the
negative half, `R5` 47k / `R6` 56k divides, and `D2` BZV55B3V3 clamps the peak. GPIO35
therefore sees a 0 V to ~3 V square wave with the CP duty cycle preserved - readable as a
**digital** signal, no ADC needed for the timing.

**Tightest margin in the design:** in state C (CP +6 V) the divider gives ~2.9 V and D2
does *not* clamp, against an ESP32 input-high threshold of ~2.48 V. States A and B are
clamped and comfortable. Since state C is where all real measurements happen, verify this
on the bench - the firmware reports `adc_raw` for exactly this purpose.

Notes:
- **GPIO35 is input-only** on the ESP32 and has no internal pull-up/pull-down - fine for reading, but it
  cannot be repurposed as an output.
- It sits on **ADC1**, so it stays usable while WiFi is active (ADC2 does not).
- The 47k/56k divider scales the CP swing into the ADC range; CP is bipolar (+12 V / -12 V), so the
  negative half is clipped by the front end.

Related: [[evsim-pilot-state-control]] for the resistor scheme and why PP is non-binding,
[[evsim-reference-gonium]] for the duty -> current formula the firmware must implement.
