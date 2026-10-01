---
name: evsim-amtron-interfaces
description: What the AMTRON Charge Control's web config actually exposes - REST enabled without UID, HTTPS off, Modbus TCP available but off, SEMP on
metadata:
  type: project
---

Confirmed from the AMTRON Charge Control webconfig (screenshots, 2026-09-07). The unit runs a
Phoenix-Contact-style charge controller web interface.

**System / General**
- `REST interface` = **Enabled (without UID)** -> REST is on and needs **no auth token**.
- `Enable HTTPS` = **Off** -> plain HTTP.
- `Log Level` = LOG_LEVEL_DBG; a built-in tcpdump start/stop is available (useful for reverse-engineering).

**Local / energy management**
- `Operator Current Limit [A]` = 0  <- the likely target field for the max-current control.
- `Energy management from external input` = Disable
- `Modbus TCP Server for energy management systems` = **On since 2026-10-01** (port 502, address set MENNEKES); see [[evsim-amtron-modbus]]
- `SEMP interface (SMA Sunny Home Manager)` = **On**, Charging Mode = *Surplus charging*,
  fallback current 16 A, 600 s to connection failure
- `EEBus interface` = Off; `Dynamic Load Management` = Disabled; ASKI over OCPP-S = Off
- OCPP SmartCharging profiles supported

**Alternative control paths besides REST:** Modbus TCP, SEMP, EEBus, OCPP smart charging, DLM.

**Watch out:** SEMP is currently ON in *surplus charging* mode - it may fight or override a
REST-set current limit. Consider turning it Off while testing.

Related: [[evsim-tb-overview]], [[evsim-inverter-fronius]].
