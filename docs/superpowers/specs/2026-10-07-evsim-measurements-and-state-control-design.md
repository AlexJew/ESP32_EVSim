# EVSim Page: Full Measurements (A / V / W) and Vehicle-State Control — Design

**Date:** 2026-10-07
**Status:** draft, awaiting review
**Scope:** finish the redesigned page (`server/charger_web/`, the "new layout" on `feat/new-layout`). It gets (1) the missing measurements: current, voltage and power for the car charger, the inverter's DC input and the inverter's AC output, in the Measurements list and on the chart; and (2) the vehicle-state control, a dropdown that commands the board's pilot state over MQTT. Amends
[2026-10-01-evsim-dashboard-design.md](2026-10-01-evsim-dashboard-design.md) (where it says the page publishes nothing and that the inverter side is power only) and
[2026-09-30-inverter-writer-design.md](2026-09-30-inverter-writer-design.md) (the `evsim/inverter` message). Where they disagree, this document wins.

## Decisions (from the user, 2026-10-07)

- Every measurement is a triple **current A / voltage V / power W**. For three-phase quantities, the current and the voltage are the **mean of the three phases**.
- The setpoint server's `evsim/inverter` message may gain fields; the page reads the inverter's values from it and still never talks to the inverter.
- **"Validated" means the board accepted the command.** The page does not wait for the charger's own state to follow.
- The vehicle state is chosen in a **dropdown** (A, B, C), not a stepper.
- The page therefore gains **one write path to MQTT**: the state command. The charger limit stays the only Modbus write.
- Porting the chart zoom (`feat/chart-zoom`) onto the new layout is a separate, later step.

## Data sources

### Car charger (AMTRON, Modbus, existing poller)

Read-only probe on 2026-10-07, values consistent with each other (the three phase powers sum to 8 243 W against a total of 8 245 W; current times voltage gives 2 734 W per phase against 2 737 W). The register meanings are **inferred from those values**, not read from the specification text.

| Register | Meaning | Size |
|---|---|---|
| 220 | total active power, W | 32 bits, high word first |
| 222, 224, 226 | phase voltage L1, L2, L3, V | 32 bits each |
| 212, 214, 216 | phase current, mA | already read |

`amtron_registers.json` gains `meter_power_w` (220) and `meter_voltage_l1_v`, `_l2_v`, `_l3_v` (222, 224, 226), with the same "0xFFFFFFFF means no meter" rule as the currents. The poll goes from 9 to 13 reads every 2 s, one connection, as now. `status()` gains `meter_power_w` (a reading) and `meter_voltage_v` (a reading holding the phases, like `meter_current_a`). The service never writes anything new.

### Inverter (setpoint server, `evsim/inverter`)

`Inverter.power()` (setpoint server) already reads SunSpec model 103 and model 160 each second. It additionally returns, from model 103, `ac_a` (the mean of the three phase currents) and `ac_v` (the mean of the three phase-to-neutral voltages); from model 160 it already has MPPT 1's `dc_a` and `dc_v`. Scale factors are applied exactly as for the power values.

The message gains four fields, `dc_a`, `dc_v`, `ac_a`, `ac_v`, each a number rounded to 0.1 or `null` if unreadable, next to the existing `dc_w` and `ac_w`. The change is additive: nothing that reads the message today breaks. `tools/` and the inverter-writer spec's field table are updated.

### Telemetry (page side)

- `parse_inverter` returns `dc_a`, `dc_v`, `ac_w`, `ac_a`, `ac_v` as well. Each is a finite non-negative number within a sanity bound (A up to 100, V up to 1 000, W up to 30 000) or `None`; a bad one becomes `None` and the message is kept, as for `dc_w`.
- `snapshot()["inverter"]` becomes `{"dc_w", "dc_a", "dc_v", "ac_w", "ac_a", "ac_v"}`, each a reading, all from the same message, so all go stale together.
- The subscriber also subscribes to `evsim/reply` (see state control). Nothing else changes: `evsim/cp`'s `state` stays the board's presented state.

## Page

- **Measurements list:** three rows, each "`11.8 A / 232 V / 8245 W`": Car charger, DC input, AC output. A value that is not available shows a dash in its place. A row dims as one, using its power reading's status.
- **Chart:** the quantity dropdown gains a third entry, **Voltage [V]**. Power: Board, EV setpoint, Car charger, DC input, AC output. Current: Signalled, Car charger, DC input, AC output. Voltage: Car charger, DC input, AC output. The line colours, the shared hide/show toggles and the "no data" hint are unchanged. The y-axis for volts steps by 100 V from 0.
- **History becomes the source for all three charts.** The browser-side current sampling (one sample per charger poll since the page opened) is removed: the current chart has the same server-side 10 minutes at 1 Hz as the power chart. Frames gain `charger_w`, `charger_a`, `charger_v`, `sig_a`, `dc_a`, `dc_v`, `ac_w`, `ac_a`, `ac_v` (`cp_w`, `setpoint_w`, `target_dc_w`, `dc_w` stay). A value is recorded only if its reading is `ok`, else `None`. `charger_a` and `charger_v` are the mean of the phases that read; they need the meter reading to be `ok`. `History` gets the charger service as a second source (its cached `status()`, no Modbus access from the sampler).

## Vehicle-state control

- **Dropdown** with the options A, B, C, in the control card. Its value follows the board's presented state (`evsim/cp` `state`) while no command is pending. With the broker down, or no state known, it shows a dash and is disabled.
- **Choosing an option** sends `POST /api/vehicle/state` with `{"state": "A" | "B" | "C"}`: same protections as the limit write (loopback, Host and Origin checks, `application/json`, 1 KiB, exactly one key, uppercase letter required).
- **The server** publishes the letter to `evsim/cmd/state` (**QoS 0**, not retained: paho queues a QoS 1 message it cannot send and replays it after a reconnect, so a command shown as failed could be carried out later, and an A stops the charger) through the page's existing MQTT client, then waits up to **3 s** for the board's answer on `evsim/reply`. Replies are one line and carry no command id, so it matches by content and ignores unrelated ones:
  - accepted: `state X -> <requested>` or `already in <requested>` → **200** `{"success": true, "state": "<requested>"}`;
  - refused: `refused X -> <requested>: <reason>` (for example A to C without B first) → **409** `{"success": false, "error": "<reason>"}`;
  - no matching reply in 3 s → **503** `{"success": false, "error": "The board did not answer."}`;
  - broker not connected → **503** at once, nothing published.
- **One command at a time:** a second request while one is waiting gets **409** "A command is already in progress."
- **The status pill** (shared with the current slider) shows the outcome of the last command of either kind: "Validated" or "Not validated", the reason in its tooltip. After a refusal or a failure the dropdown returns to the board's presented state.
- **Consequences to know:** going to A opens the pilot, so the charger stops and the inverter loses its DC input; it needs about two minutes to start again (`memory/evsim-inverter-fronius.md`). There is no confirmation dialog.

## Error handling

| Situation | Behaviour |
|---|---|
| Charger meter not fitted or unreadable | The car-charger row shows dashes and dims; its chart lines have gaps. |
| Setpoint server stopped, or run without `--inverter` | The DC and AC rows go stale (dimmed, last values kept) and their lines have gaps, as for the DC power today. |
| Inverter unreachable by the server | `dc_*` and `ac_*` are `null`: the rows show dashes. |
| Old setpoint server (no new fields) | The new inverter values read `None`; nothing breaks. |
| Broker down | The dropdown is disabled; a POST answers 503 without publishing. |
| Malformed `evsim/reply` | Ignored, never an exception. |
| Wrong state value or body in the POST | **400**, nothing published. |

## Testing

- **Setpoint server:** `Inverter.power()` returns `ac_a` and `ac_v` as means with the scale factors applied (against the fake inverter, extended with phase values); the status message carries the four new fields, `null` when unreadable; the existing message tests still pass.
- **Charger service:** the new registers decode, the "no meter" value gives `None`, `status()` carries them; the poll still never writes.
- **Parsers and telemetry:** the new `evsim/inverter` fields (good, bad, missing); `evsim/reply` is subscribed again after a reconnect.
- **History:** the new frame keys; means of the phases; a stale source is `None`; the charger source is read from its cache.
- **Command path:** accepted, "already in", refused, timeout, broker down, a busy second request, an unrelated reply ignored, a wrong requested letter; the POST's Host, Origin, body and type checks; nothing is published for a rejected request.
- **Page, in Node against the stub DOM:** the three-part rows, the dropdown (follows the board, disabled when unknown, sends on change, restores after a refusal), the voltage chart and its legend, the current chart fed from history.
- **Live check:** with the real rig, the three rows against the AMTRON's page, the inverter tool and the setpoint server's message; one state command B → C → B on the board (the charger is already in state C, and a move to A is **not** part of the check).

## Not in scope

Per-phase rows or lines; a confirmation dialog for state A; waiting for the charger's state to follow a command; correlating replies by command id (needs a firmware change); porting the chart zoom; the amps-to-watts conversion; authentication.
