---
name: evsim-open-questions
description: EVSim behaviours confirmed and still open as of 2026-09-07 - the no-load current-offer oscillation is EXPECTED, plus the EEBus HEMS on the LAN
metadata:
  type: project
---

## Confirmed normal: the no-load oscillation (do NOT chase this as a bug)

With the protoboard presenting state C but **no actual load drawing current**, the charger cycles on a
roughly **6-12 second period**, polled 2026-09-07 over 40 s:

| phase | `SignaledCurrentLimit_vehicleif` | `OCPPStateConnector1_ocpp` |
|---|---|---|
| on  | = the operator limit (10 A) | `CHARGING (occupied)` |
| off | `0 A`                       | `CHARGING (faulted)`  |

`Type2StateConnector1_vehicleif` stays `(C) Vehicle connected ready` the whole time; meter current stays
0.00 on all phases. The charger offers current, sees nothing drawn, faults, retries.

**This was Alexandre's own diagnosis and it is correct.** An earlier guess that SEMP surplus-charging was
holding current at 0 was **wrong** - the oscillation happens regardless. Expect it to settle once the
inverter actually draws load, i.e. the flapping resolves itself when the control loop closes.

**Design consequences for the ESP32:**
- It **must tolerate the current offer vanishing** every few seconds. Naively converting every sample into
  a setpoint would publish 0 W periodically and make the inverter oscillate. Hold the last valid value, or
  gate on CP state / a validity flag.
- Calibration must be done **during the on-windows only**.

## Confirmed: REST -> CP signal path works end to end
`OperatorCurrentLimit_vehicleif` (set over REST) and `SignaledCurrentLimit_vehicleif` (read from the
dashboard) track **1:1**. At 10 A the expected CP duty is `10 / 0.6` = **16.7 %** - the number to check the
ESP32's PWM measurement against. See [[evsim-amtron-live-telemetry]].

## Still open
- An **EEBus HEMS is configured on the LAN**: `EEBUSCoupledEnergyManager_eebus` names *"Helios Home HEMS"*
  at **192.168.188.158** (`coupled: 0`, EEBus Off). Another energy manager that could compete for control.
- The charger has its own **MQTT client** for hierarchical DLM (`DlmcMqttBrokerAddress_dlmc`, port 8883) -
  for DLM master/slave between chargers, but proof the firmware speaks MQTT natively.
- Whether the Fronius GEN24 can consume an MQTT setpoint at all - see [[evsim-inverter-fronius]].
