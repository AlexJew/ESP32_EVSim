---
name: evsim-reference-gonium
description: https://evsim.gonium.net/ - the process reference for the EVSim TB, incl. the CP duty-cycle-to-current formulas and PP resistor table; note it is a PASSIVE analog board with no REST/MQTT
metadata:
  type: reference
---

https://evsim.gonium.net/ - the process reference the EVSim thesis follows. Implements DIN EN 61851-1:2012
and SAE J1772.

**Important difference:** gonium's EVSim is *standalone analog hardware* with toggle switches and no
digital networking at all. The REST + MQTT + ESP32 layers in [[evsim-tb-overview]] are this thesis's own
contribution, not something to look up there.

Key values taken from it:
- CP states: A = 12 V DC (no EV); B = 9 V / -12 V at 1 kHz; C = 6 V / -12 V at 1 kHz. States A-F defined.
- CP duty -> current: `I[A] = 0.6 * duty%` for 6-51 A; `I[A] = 2.5 * (duty% - 64)` for 51-80 A.
  Valid duty range 5-96 %.
- PP resistor (PP-PE) encodes cable rating: 100R=63A, 220R=32A, 680R=20A, 1500R=13A.
- Board parts: R1 2k7, R2 1k2, R3 (per current rating), 1N4007 diode.

**How to apply:** the duty->current formulas are the conversion the ESP32 must implement before publishing
the power setpoint. As of 2026-09-07 no MENNEKES REST/protocol docs exist for the project.
