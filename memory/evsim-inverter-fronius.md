---
name: evsim-inverter-fronius
description: The EVSim load-side device is a Fronius GEN24 6.0 Plus inverter, intended to adapt its load from the ESP32's MQTT power setpoint
metadata:
  type: project
---

The inverter in the EVSim chain is a **Fronius Symo/Primo GEN24 6.0 Plus**. It is the consumer of the
power setpoint the ESP32 publishes over MQTT (see [[evsim-tb-overview]]).

**Open risk to verify on the real unit:** the GEN24 exposes **Modbus TCP/RTU (SunSpec)** for control and a
read-only **Solar API (REST)**; it is not known to subscribe to arbitrary MQTT topics for a power setpoint.
If confirmed, the architecture needs an **MQTT -> Modbus TCP bridge** between the ESP32 and the inverter
rather than a direct subscription.

**How to apply:** confirm the GEN24's control interface before designing the outbound message format -
this decides whether the ESP32 talks MQTT-only or whether a broker-side bridge is part of the deliverable.
