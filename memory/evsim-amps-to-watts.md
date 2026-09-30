---
name: evsim-amps-to-watts
description: Decision (2026-09-30), not yet implemented - turn the offered current into the inverter's DC power target with 1.2247 x amps x measured DC voltage (about 630 W/A) instead of amps x 690 V
metadata:
  type: project
---

**Decision:** the DC power the inverter is told to take from the charger should be

    target_dc_w = 1.2247 x I_offered x V_dc_measured        (about 630 W per offered amp at 515 V)

not `I_offered x 690`. This is "option A" from the 2026-09-30 discussion. It is **not implemented yet**.

**Why:** the board turns the offered current into watts as amps x 690 V (`kWattsPerAmp` = 3 x 230 V in
`EVSim/lib/cp_convert/cp_convert.h`, chosen to match the AMTRON's own EEBus failsafe value of 11 040 W at 16 A). That
is the nominal AC power a three-phase load would take. In our rig the current goes through a rectifier into the
inverter's DC input, where two things differ:

- a three-phase bridge turns the per-phase AC current into DC current with a factor of sqrt(3/2) = **1.2247**;
- the rectified DC voltage is about **510 to 520 V** under load (566 V open circuit), not 690 V.

So the same offered current is about 1.2247 x 515 = **630 W per amp** of DC power. Following the 690 V figure makes the
inverter take about **7 to 10 % more current than the charger offered**: at 16.2 A the board says 11 190 W, which at
511 V is 21.9 A DC = 17.9 A per phase against 16.2 A offered; at 12 A the board says 8 280 W, about 13.1 A per phase.
Measured 2026-09-30 ([[evsim-inverter-fronius]], extended sweep): at a 16.2 A offer the DC current pinned at 19.1 A,
because the source stops near 16 A per phase, which hid the overshoot at the top only. A real EV must never exceed the
offered current.

## How to apply (when this is implemented)

1. **Leave the firmware and the `evsim/cp` message alone.** `setpoint_w` stays in board watts (nominal AC, amps x 690),
   and the hold and zero logic in the firmware keeps working on it. Other consumers of `evsim/setpoint` are unaffected.
2. **Convert in the server.** The averaged `setpoint_w` divided by 690 is the averaged offered current in amps (put
   the 690 in `config.py` as a named constant with a comment that it must match `kWattsPerAmp`). The writer then
   computes `target_dc_w = 1.2247 x amps x V_dc` and hands that to the existing `ac_cap_for_dc`. The calibration
   (AC cap versus DC power) is unchanged.
3. **Take `V_dc` from the inverter.** `Inverter.power()` already returns the MPPT 1 DC voltage. Use a smoothed value
   (for example a mean over the last 5 to 10 s) so the target does not jitter, and **use a fixed fallback of 515 V
   whenever the inverter is not running or the DC current is below about 1 A**. The open-circuit voltage (566 V) or a
   start-up reading would inflate the target by 10 %. The dead-band (100 W) already absorbs the small feedback between
   the cap, the current and the voltage sag.
4. **Zero stays zero.** A 0 A offer, state A/B and no data behave exactly as before; only non-zero targets change.
5. **Status.** `evsim/inverter` already reports `target_dc_w`; it will now differ from `setpoint_w` (about 9 % lower).
   Document that, and add the offered amps and the `V_dc` used to the status if it helps debugging.
6. **Docs to update with it:** the inverter writer spec (the definition of the setpoint in "Purpose"), the README, and
   this note's status.

## Tests to write

- A pure function `dc_target_w(amps, v_dc)` (1.2247 x amps x v_dc), with the 515 V fallback when the inverter is not
  running or draws under about 1 A.
- Writer tests against the fake inverter with a chosen `dc_v`: the target and the register follow the measured
  voltage, the smoothing holds it steady, and the fallback applies while the fake reports a non-MPPT state.
- Example values to check: 12.2 A at 515 V gives about 7 690 W; 16.2 A at 511 V gives about 10 140 W.

## Validate on the rig afterwards

- The DC current should equal 1.2247 x the offered current within about 2 %, at several offers (for example 8, 10,
  12 A). Log it with `tools/fronius_modbus.py watch` next to the offered amps.
- Compare with the AMTRON's own meter (the REST dashboard has a power reading). The wiring, rectifier and transformer
  between the charger and the inverter lose about 10 % at full current (an effective 2.5 ohm), so the charger's meter
  will read a bit more than the inverter's DC input. That gap is a property of the rig, not an error in the target.

## Open points

- The factor 1.2247 assumes a **balanced three-phase full-wave bridge**; confirm the rectifier and check the per-phase
  currents on the AMTRON's meter.
- If the thesis wants the loop to match the charger's meter instead of the DC input, add the loss (about 2.5 ohm x
  I_dc squared) to the target, or calibrate against the meter. Not decided.
- Regulating the DC current directly (option B in the discussion) was set aside as more complex than needed.
