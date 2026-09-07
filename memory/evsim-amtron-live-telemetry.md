---
name: evsim-amtron-live-telemetry
description: GET /json/dashboard.json gives live CP state and signaled current - the ground truth for validating the ESP32's PWM duty-to-current conversion
metadata:
  type: reference
---

`GET /json/dashboard.json` with the `Authorization:` token (see [[evsim-amtron-rest-api]]) returns ~19 KB
of live read-only telemetry. Same `groups[].fields[]` shape as settings.

**The two fields that matter most for this thesis:**
- **`Type2StateConnector1_vehicleif`** - the IEC 61851 state the charger *thinks* it sees,
  e.g. `"(C) Vehicle connected ready..."`. Confirms whether the MOSFET resistor switching worked.
- **`SignaledCurrentLimit_vehicleif`** - the current the charger is signalling on CP, in A.

**Why this is valuable:** `SignaledCurrentLimit_vehicleif` is the charger's own statement of what its CP
PWM means. It is the **ground truth to validate the ESP32's duty -> current conversion** against - set an
operator limit over REST, read this field, and compare with what the ESP32 computed from the PWM. That
closes the loop without an oscilloscope.

Other useful fields: `OCPPStateConnector1_ocpp`, `OcppMeterCurrent_meter` (per-phase A),
`OcppMeterFrequency_meter`, `RCMBState_rcmb`, `Type2NumberContactorCyclesRO_vehicleif` (contactor wear),
and `StartChargeViaWebInterface_webcharging` / `StopChargeViaWebInterface_webcharging` (web start/stop).
