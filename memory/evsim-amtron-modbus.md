---
name: evsim-amtron-modbus
description: The AMTRON's Modbus TCP interface as measured 2026-09-30 - register map, 30 s fallback to 0 A, the limit restored on the next request; the charger page uses it instead of REST
metadata:
  type: project
---

The AMTRON Charge Control (CC612, firmware 5.33, protocol 0.14) has a Modbus TCP server, switched on in its web
config (port 502, register address set MENNEKES, Allow Start/Stop Transaction Off). Unit ID 255 (any 1 to 255 works).
The map is MENNEKES's published ECU Modbus TCP specification, rev 1.07, which names AMTRON Charge Control. Design:
`docs/superpowers/specs/2026-10-01-charger-modbus-backend-design.md`.

**Every register is a holding register (function code 3)**; function code 4 is refused. Register 130 does not exist.

| Register | Meaning |
|---|---|
| 1000 | HEMS current limit, read/write, A. **0 pauses charging.** The only register the page writes. |
| 134 | Operator limit (set on the AMTRON page), **read only**. The effective limit is the lower of 134 and 1000. |
| 133 | Reads 16: probably the installation limit (not in the spec). |
| 706 | Signalled current, A. |
| 212, 214, 216 | Per-phase current in mA, 32-bit, high word first; 0xFFFFFFFF means no meter. |
| 122, 104 | Vehicle state (A=1, B=2, C=3), OCPP status (1 Occupied, 6 Charging, 7 Suspended by the charger). |
| 131, 132 | Safe current (0 A) and communication timeout (30 s). |

**Behaviour that matters (measured):**
- Writing 1000 = 10 gave a signalled 10 A within about 3 s, and the board saw a 10.19 A pilot.
- After **30 s without Modbus traffic the charger falls back to 0 A**, and register 1000 reads 0.
- The charger **remembers the last limit written and restores it on the next request after a fallback, even a plain
  read**, within a few seconds. Reading the charger is therefore not neutral: it can resume charging.
- Plain reads keep the limit alive (70 s of reads only, past the 30 s timeout), so a polling service needs no
  separate heartbeat.

**Why Modbus and not REST:** the REST login is a single session shared with the AMTRON's own page, so the two
fought over it. Modbus has no login. It is also unauthenticated, so anything on the lab network can set the current.

Related: [[evsim-amtron-interfaces]], [[evsim-amtron-rest-api]], [[evsim-inverter-fronius]].
