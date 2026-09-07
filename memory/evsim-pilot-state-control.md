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
