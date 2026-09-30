---
name: evsim-network
description: EVSim lab network - SSID hems, ESP32 static 192.168.21.138/24, AMTRON 192.168.21.117, Fronius 192.168.21.120, gateway 192.168.21.130, no DHCP and no internet
metadata:
  type: project
---

The laboratory WLAN the EVSim board joins. **There is no DHCP server**, so the ESP32's address is
configured in firmware ([[evsim-firmware-layout]]).

| Role | Address |
|---|---|
| SSID | `hems` |
| ESP32 (EVSim board) | `192.168.21.138` |
| AMTRON charger | `192.168.21.117` |
| Fronius GEN24 12.0 Plus SC inverter | `192.168.21.120` (an earlier note had `.122`) |
| WLAN gateway | `192.168.21.130` |
| Subnet mask | `255.255.255.0` (`/24`) |

**All three devices in the loop - charger, ESP32, inverter - share the `/24`**, so traffic between
them never leaves the subnet and never depends on the gateway. A wrong gateway cannot break the
thesis signal chain.

## Two constraints that bind every later design

**No internet route.** No NTP, so the firmware has **no wall-clock time** - `millis()` remains the
only timebase, as the CSV already assumes. Nothing may depend on a real date.

**DNS is unconfirmed** (Alexandre had not yet checked as of 2026-09-25). Therefore **every peer is
addressed by IP, never by hostname** - the MQTT broker, the Fronius inverter
([[evsim-inverter-fronius]]) and the charger's REST interface ([[evsim-amtron-rest-api]]) included.
This keeps a dead or absent DNS server from costing anything.

**Why it matters:** this is what unblocks the MQTT bridge ([[evsim-pilot-state-control]]), the last
missing link in the chain described in [[evsim-tb-overview]].

Design record: `docs/superpowers/specs/2026-09-25-wifi-static-ip-design.md`.
