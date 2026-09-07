---
name: evsim-pilot-state-control
description: How the EVSim PCB fakes EV pilot states A/B/C - two MOSFETs switching 2k7 and 1k2 resistors on CP, driven by an MQTT command topic
metadata:
  type: project
---

The ESP32 emulates the EV's CP resistor network with **two MOSFETs used as switches**, presenting the
IEC 61851 resistances:
- **2k7 Ohm** -> state **B** (EV connected, not ready to charge)
- **1k2 Ohm** (in parallel, total ~882 Ohm) -> state **C** (EV connected, ready to charge)
- neither -> state **A** (no EV)

State transitions A -> B -> C are commanded remotely: the ESP32 **subscribes** to an MQTT topic whose
messages drive the MOSFET gates. A second, separate MQTT topic is what the ESP32 **publishes** the derived
power setpoint on for the inverter.

**Why:** two independent MQTT directions - inbound = pilot state control, outbound = power setpoint. Do not
conflate them into one topic.

**How to apply:** see [[evsim-tb-overview]] for the full chain.

## PP is deliberately non-binding

A fixed **220 Ohm** resistor sits between **PP and PE**, which per the IEC 61851 table encodes a **32 A**
cable. The charger confirms this - the dashboard's `Type2StateConnector1_vehicleif` string reads
`PR: 32 A Plug locked`.

**Why:** 32 A is far above the charger's 16 A `InstallationCurrentLimit_vehicleif`, so the proximity pilot
**never constrains the current**. Control of the charging current sits **entirely with the AMTRON**, signalled
over CP. This is intentional, not incidental: it removes PP as a variable so every observed current change
is attributable to the REST command alone - one input, one output.

**How to apply:** never treat PP as part of the control loop, and do not "fix" the 220 Ohm value. If a
future test needs PP to bind, the gonium table is 100R=63A, 220R=32A, 680R=20A, 1500R=13A
([[evsim-reference-gonium]]).
