---
name: evsim-tb-overview
description: EVSim bachelor thesis (TB) - ESP32 PCB simulates an EV toward a MENNEKES AMTRON Charge Control 11 kW charger, relaying a power setpoint to an inverter over MQTT
metadata:
  type: project
---

Bachelor thesis (Travail de bachelor HES) project `EVSim`, PlatformIO/Arduino on `esp32dev`, at
`~/Desktop/Cours/TB - Travail de bachelor HES/Code/ESP32_EVSim/EVSim`.

Hardware: MENNEKES **AMTRON Charge Control 11 kW** wallbox. A custom PCB connects the ESP32 directly to
the charger's **CP / PP pilot lines** (EV side simulation). The charger's power cables feed an **inverter**
that acts as the simulated load.

End-to-end signal chain:
1. A web interface sets the **maximum allowed current** on the charger via its **REST interface**.
2. The charger encodes that limit as the **CP PWM duty cycle**; the ESP32 measures the PWM and converts
   duty -> amperes -> power setpoint.
3. The ESP32 **publishes the power setpoint over MQTT**; the inverter subscribes and adapts its load.

**Why:** this is the whole point of the thesis - closing the loop from a REST command to a real inverter
load without a physical EV.

**How to apply:** treat CP PWM measurement, the REST client, and the MQTT bridge as the three core
subsystems. See [[evsim-pilot-state-control]] and [[evsim-reference-gonium]].
