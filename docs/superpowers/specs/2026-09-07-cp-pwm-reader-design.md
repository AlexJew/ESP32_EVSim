# CP PWM Reader — Design

**Date:** 2026-09-07
**Status:** approved
**Scope:** ESP32 firmware that measures the Control Pilot PWM duty cycle on GPIO35 and
converts it to a current and power setpoint, reported over serial.

## Purpose

The MENNEKES AMTRON signals the maximum current it allows as a PWM duty cycle on the
Control Pilot line. This firmware measures that duty cycle and converts it into a power
setpoint. Publishing the setpoint over MQTT and driving the pilot states are deliberately
out of scope here — see *Non-goals*.

Success means the duty cycle measured by the ESP32 agrees with the charger's own
`SignaledCurrentLimit_vehicleif`, read over REST by `tools/charger_poll.py`, across the
range 6–16 A.

## Hardware context

Front end between Control Pilot and GPIO35, per `PCB_schematics.kicad_sch` rev 1.0:

```
J5.1 (CP) → D1 (1N4007) → node ─┬─ R1 2k7 → Q1 (2N7000) → GND     [state B, GPIO33]
                                ├─ R3 1k2 → Q2 (2N7000) → GND     [state C, GPIO32]
                                └─ R5 47k ─┬─ GPIO35
                                           ├─ R6 56k → GND
                                           └─ D2 (BZV55B3V3) → GND
J5.3 (PP) → R7 220 → GND                                          [fixed 32 A, non-binding]
```

D1 removes the negative half of the bipolar CP signal; R6 pulls the divider tap to 0 V
during it. D2 clamps the positive peak. GPIO35 therefore sees a unipolar square wave that
preserves the CP duty cycle.

Levels at GPIO35, assuming ~0.6 V across D1:

| CP state | CP high | At GPIO35 | Clamped by D2 |
|---|---|---|---|
| A | +12 V | ~3.0–3.3 V | yes |
| B | +9 V | ~3.0–3.3 V | yes |
| C | +6 V | **~2.9 V** | **no** |
| negative half | −12 V | 0 V | D1 blocks |

**State C is the tightest margin in the design** and the state in which all real
measurements happen: ~2.9 V against an ESP32 input-high threshold of ~2.48 V, leaving
~0.4 V. This must be confirmed on the bench. GPIO35 is `ADC1_CH7`, so the firmware can
report a raw ADC reading alongside the digital measurement to verify the level directly.

GPIO32 and GPIO33 each carry a 100 kΩ gate pulldown (R8/R9), so the board powers up in
state A regardless of GPIO float during boot.

## Non-goals

- **No MQTT.** Added once the measurement is validated against the charger.
- **No driving of GPIO32/GPIO33.** This firmware is strictly read-only, so measurement can
  be validated before anything changes charger state.
- **No control loop.** Converting a setpoint into inverter behaviour is a later stage.

## Architecture

Four units, split so the arithmetic is testable without hardware.

### `lib/cp_convert/` — pure conversion, no Arduino dependency

Free functions over plain types, compiled for both the ESP32 and the host:

- `CpState classify(float duty_pct, float freq_hz, bool level_high, bool has_edges)`
- `float duty_to_current(float duty_pct)` — amps, or NaN outside the valid range
- `float current_to_power(float amps)` — watts

`CpState` is one of `NO_SIGNAL`, `DC_12V`, `PWM_VALID`, `OUT_OF_RANGE`.

Conversion, per IEC 61851-1 / evsim.gonium.net:

| Duty | Meaning |
|---|---|
| > 96 % | DC / no limit — `DC_12V` |
| 85 % < d ≤ 96 % | `I = 2.5 × (d − 64)` — 51 to 80 A |
| 10 % ≤ d ≤ 85 % | `I = 0.6 × d` — 6 to 51 A |
| 3 % ≤ d < 10 % | below the 6 A floor — `OUT_OF_RANGE` |
| < 3 % | `NO_SIGNAL` |

Power assumes three-phase at 230 V per phase: `P = 3 × 230 × I = 690 × I`.

This is confirmed against the charger rather than assumed: 690 × 16 A = 11040 W, matching
the `EEBusLPCFailsafePowerUser_eebus = 11040` value read from the AMTRON's own settings.

### `src/cp_reader.*` — edge capture

Owns GPIO35 and nothing else. An ISR on `CHANGE` timestamps edges with `micros()` and
accumulates high-time, total-time and an edge count into volatile counters. A `sample()`
call snapshots and resets them with interrupts briefly disabled, returning duty, frequency
and edge count over the elapsed window.

At 1 kHz a 200 ms window covers ~200 periods, which averages out ISR jitter and gives
roughly 0.1 % duty resolution.

**Rejected alternative:** the MCPWM capture unit timestamps in hardware at 80 MHz and is
immune to ISR latency. It is the correct upgrade *if* WiFi disturbs the timing once MQTT
is added, but it is more code for a problem that may not appear. Revisit only if measured
jitter demands it.

### `src/main.cpp`

Initialises serial and the reader, then every 200 ms samples, converts, and prints one CSV
row.

### `test/test_cp_convert/`

Native unit tests for the conversion functions, run on the host with `pio test -e native`.
No hardware required.

## Data flow

```
CP PWM → front end → GPIO35 → ISR edge timestamps → 200 ms accumulator
       → duty %, freq → classify() → duty_to_current() → current_to_power()
       → CSV over serial
```

## Output format

CSV on serial at 115200 baud, sharing column names with `tools/charger_poll.py` so the two
logs can be merged on timestamp to produce the calibration curve:

```
millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw
```

`current_a` and `power_w` are empty when the state is not `PWM_VALID`. A header line is
printed once at boot.

## Error handling

The charger holds no valid offer for several seconds at a time when nothing draws load —
observed cycling on a 6–12 s period. This is normal, not a fault, and the firmware must
represent it rather than smooth it away:

- **No edges, pin high** → `DC_12V`. Emit the row with empty current and power.
- **No edges, pin low** → `NO_SIGNAL`.
- **Duty outside the valid range** → `OUT_OF_RANGE`, no current or power emitted.
- **Frequency far from 1 kHz** (outside 900–1100 Hz) → `OUT_OF_RANGE`; the signal is not CP.

No value is ever invented for an invalid state. Downstream stages must decide how to hold
the last good setpoint; that decision does not belong in the reader.

## Testing

**Native unit tests** cover every branch of the conversion table: each range boundary
(3, 10, 85, 96 %), both current formulas, the 16 A → 11040 W anchor, and NaN outside range.

**On-hardware validation** is the real acceptance test: run the firmware and
`tools/charger_poll.py --sweep 6,8,10,13,16` at the same time, then compare measured duty
against the charger's signalled current. At 10 A the expected duty is 16.67 %.

## Resolved: GPIO34 is not available

GPIO34 (`VDET_1`) appears as a stub in the schematic but **was not connected on the
fabricated PCB**. ADC sampling therefore happens on GPIO35 itself, alongside the digital
edge timing on the same pin. `analogRead()` on GPIO35 gives a raw count reported in the
`adc_raw` column, which is enough to confirm the state C level margin on the bench.

Sampling the ADC on the same pin the ISR watches is safe here: `analogRead()` does not
change the pin's digital function, and it is called once per 200 ms window rather than
inside the ISR.
