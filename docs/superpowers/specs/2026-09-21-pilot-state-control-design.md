# Pilot State Control — Design

**Date:** 2026-09-21
**Status:** approved
**Scope:** ESP32 firmware that drives GPIO33 and GPIO32 to present IEC 61851 pilot states A, B
and C to the charger, commanded over serial.

## Purpose

The EVSim board emulates an EV toward the MENNEKES AMTRON by switching resistors onto the
Control Pilot line. This firmware drives the two MOSFET gates that do the switching, so the
charger can be taken through A → B → C without a physical vehicle.

It extends the read-only CP PWM reader
(`2026-09-07-cp-pwm-reader-design.md`), which deliberately left GPIO32/GPIO33 untouched so
measurement could be validated first. Commanding from MQTT is out of scope here — see
*Non-goals*.

## Hardware context

Per `PCB_schematics.kicad_sch` rev 1.0 (same mapping on the protoboard):

| State | Resistance on CP | GPIO33 → Q1 (2k7) | GPIO32 → Q2 (1k2) |
|---|---|---|---|
| A — no vehicle | none | low | low |
| B — connected, not ready | 2k7 | high | low |
| C — connected, ready | 2k7 ∥ 1k2 ≈ 882 Ω | high | high |

State C is the 1k2 **in parallel with** the 2k7, not a replacement for it, so Q2 alone would
present 1k2 and never a valid state. GPIO32 may therefore only be high while GPIO33 is high.

R8 and R9 (100 kΩ) pull both gates low, so the board is in state A at power-up and through
reset, before any code runs.

## Non-goals

- **No MQTT command input.** The state-machine API is transport-agnostic, so MQTT later adds
  a second caller rather than a rewrite.
- **No fail-safe timeout.** If the serial sender goes away the state stays as commanded. An
  automatic drop to A would look like an unplug to the charger. The right policy depends on
  the transport (an idle terminal is not a lost broker), so it is decided when MQTT arrives.
- **No electrical fault detection.** The firmware cannot tell whether Q1 or Q2 actually
  conducted. Verification is external: the charger's `Type2StateConnector1_vehicleif`, logged
  by `tools/charger_poll.py`.
- **No gating on the measured CP.** The interlock rests on the commanded state only. GPIO35
  cannot distinguish A from B (D2 clamps both), and the charger's 6–12 s no-load offer
  cycling makes "valid PWM seen recently" ambiguous.
- **No persistence.** The state is always A after reset.

## Rules

**Transitions.** The only refused request is **A → C**, because B has not been commanded. It
is reported and the pins are left untouched. Every other transition is accepted, including
C → A directly, which is what an unplug looks like. Requesting the current state is a no-op.

| From \ To | A | B | C |
|---|---|---|---|
| A | no-op | accept | **refuse** |
| B | accept | no-op | accept |
| C | accept | accept | no-op |

**Write order.** The two pins are written one after the other, so the order is chosen to
never leave GPIO32 high while GPIO33 is low, even for microseconds. Going up sets GPIO33 then
GPIO32. Going down clears GPIO32 then GPIO33. C → B clears only GPIO32; B → C sets only
GPIO32.

**Boot.** `pilot_driver_begin()` writes both pins low before switching them to outputs, then
the firmware reports state A.

## Architecture

Three units, split so the rules are testable without hardware.

### `lib/pilot_state/` — pure logic, no Arduino dependency

Compiled for both the ESP32 and the host.

- `enum class PilotState { A, B, C }`
- `struct PilotResult { bool accepted; PilotState state; }` — the resulting state, unchanged
  when the request is refused
- `PilotResult pilot_request(PilotState current, PilotState requested)` — applies the
  transition table
- `struct PilotPins { bool b_high; bool c_high; }` and `PilotPins pilot_pins(PilotState)` —
  the pin mapping above
- `int pilot_plan(PilotState from, PilotState to, PilotWrite out[2])` — the ordered pin
  writes that take the board from one state to the other, each `{pin, level}`, returning the
  number of writes (0 for a no-op)
- `bool pilot_parse_command(const char *line, PilotState *out)` — accepts one letter
  `A`/`B`/`C`, either case, ignoring surrounding whitespace and CR/LF; rejects anything else
  including empty and overlong lines
- `const char *pilot_state_name(PilotState)` — stable short name for logging

### `src/pilot_driver.*` — GPIO layer

Owns GPIO32 and GPIO33 and nothing else. `pilot_driver_begin()` initialises them low, and
`pilot_driver_apply(from, to)` executes the writes returned by `pilot_plan()`. It holds no
rules of its own.

### `src/main.cpp`

Gains a line reader on the serial port feeding `pilot_parse_command()`, and applies accepted
requests through `pilot_request()` and the driver. The 200 ms CP sampling loop is unchanged
and unaffected by commands. The line buffer is small and fixed; an overlong line is discarded
rather than allowed to overflow.

## Serial interface

Line-based at 115200 baud. Send `A`, `B` or `C` followed by a newline. Every reply is
`#`-prefixed so the CSV stays parseable.

| Situation | Reply |
|---|---|
| Accepted | `# state B -> C` |
| Refused | `# refused A -> C: state B must be asserted first` |
| No-op | `# already in C` |
| Unrecognised | `# unknown command 'x'` |
| Overlong line | `# unknown command (line too long)` |
| Boot | `# pilot state A`, printed with the existing banner |

The banner comment is updated to say the firmware drives the pilot states and is no longer
read-only.

## Output format

One column is appended to the existing CSV, so the merge with `tools/charger_poll.py` still
works:

```
millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw,commanded
```

`commanded` is `A`, `B` or `C`. It sits next to the measured `state` column so a mismatch,
for example commanded B but still `DC_12V`, is visible in one log.

## Data flow

```
serial line → pilot_parse_command() → pilot_request(current, requested)
            → accepted?  ─ no → "# refused …" reply, pins untouched
                         └ yes → pilot_plan(from, to) → pilot_driver_apply() → GPIO33/GPIO32
                               → "# state X -> Y" reply, commanded state updated
```

## Error handling

- **Refused transition:** reported, never applied.
- **Unknown or malformed input:** reported, pins untouched.
- **Overlong line:** discarded, with an unknown-command reply.
- **Reads unaffected:** the CP measurement keeps its 200 ms cadence regardless of commands.

## Testing

**Native unit tests** (`test/test_pilot_state/`, run with `pio test -e native`):

- **Transition table:** all nine combinations of current and requested state. Eight are
  accepted or no-ops, and A → C is refused.
- **Refusal leaves state unchanged:** after a refused A → C the resulting state is still A
  and the pin levels are still low/low.
- **Pin mapping:** A, B and C map to the levels in the table.
- **Write order:** for every transition, stepping through `pilot_plan()` never passes through
  GPIO32 high with GPIO33 low.
- **Command parsing:** `a`, `B` and ` c\r\n` are accepted; `x`, empty and overlong lines are
  rejected.

**On-hardware validation** is the real acceptance test. Run the firmware and
`tools/charger_poll.py` together, then step through:

1. **Boot:** state A, the charger reports no vehicle, CP reads `DC_12V`.
2. **`B`:** the charger reports state B and valid PWM appears on GPIO35.
3. **`C`:** the charger reports state C. Read `adc_raw` to confirm the ~2.9 V state C level,
   which closes the open margin question from the CP PWM reader spec.
4. **`A` from C:** both pins drop and the charger reports no vehicle.
5. **`C` from A:** refused, with no change on the charger.
