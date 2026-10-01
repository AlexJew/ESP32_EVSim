# AMTRON Charger Page over Modbus TCP — Design

**Date:** 2026-10-01
**Status:** approved, implemented on this branch
**Scope:** make the existing local charger page (`server/charger_web/`) read and set the AMTRON's current over **Modbus TCP instead of the REST API**, and keep the charger's current alive from a background poller. Amends
[2026-09-30-charger-web-interface-design.md](2026-09-30-charger-web-interface-design.md); wherever the two disagree, this document wins.

## Why

The REST interface needs a login, and the AMTRON allows one REST session at a time, with no credentials set up for an API client. The page and the AMTRON's own web page therefore fight over the session, and the page cannot be open at the same time as the charger's. Modbus TCP has no login and no session, so the two can be used together. Enabling it is a charger setting (confirmed on this unit: Modbus TCP server On, port 502, register address set MENNEKES, Allow Start/Stop Transaction Off).

## What was measured on the unit (2026-09-30 and 10-01)

Firmware 5.33, protocol 0.14, model `CC612_2S0R_CC` (an AMTRON Charge Control). The register map is the one in MENNEKES's published
[ECU Modbus TCP specification, rev 1.07](https://wallboxcenter.de/wp-content/uploads/2022/06/Mennekes-ECU_modbus_tcp_server_spec.pdf), and the unit answers on **unit ID 255** (any ID from 1 to 255 is accepted).

**Every register is a holding register (function code 3).** Function code 4 (input registers) is refused with exception 2 for all of them, so the `"table": "input"` entries in the old register map are wrong. Only function codes 3 and 6 are needed.

| Register | Name | Access | Meaning |
|---|---|---|---|
| 1000 | `HEMS_CURRENT_LIMIT` | read/write | The current limit an energy manager sets, in A. **0 pauses charging.** |
| 134 | `OPERATOR_CURRENT_LIMIT` | **read only** | The limit set on the AMTRON's own page, in A |
| 133 | (not in the spec) | read | Reads 16; **inferred to be the installation limit** (the REST interface reports 16 A) |
| 706 | `SIGNALED_CURRENT` | read | The maximum current signalled to the vehicle, in A |
| 212, 214, 216 | `METER_CUR_L1/L2/L3` | read | Per-phase current in **mA**, 32 bits, high word first (two registers each) |
| 122 | `VEHICLE_STATE` | read | A = 1, B = 2, C = 3 |
| 104 | `OCPP_CP_STATUS` | read | 1 = Occupied, 6 = Charging, 7 = SuspendedEVSE (paused by the charger) and so on |
| 131, 132 | `SAFE_CURRENT`, `COMM_TIMEOUT` | read/write | Fallback current (0 A on this unit) and timeout (30 s) |
| 712, 715 | `MINIMUM_CUR_LIMIT`, `MAX_CUR_EV` | read | 6 A and 20 A on this unit |

Register 130 (`MODBUS_ADDRESS_OFFSET`) is not provided by this address set, so the offset is 0. Register 134 must **not** be written: the writable limit is 1000. The effective limit is the **lower** of 134 and 1000, so the operator limit on the AMTRON's page must be at the installation ceiling (16 A) for Modbus to control the whole range.

**Behaviour observed:**

- Writing 1000 = 10 gave `SIGNALED_CURRENT` = 10 within about 3 s, and the board measured a 10.19 A pilot.
- **Fallback:** after 30 s with no Modbus communication the charger signals `SAFE_CURRENT` (0 A) and reports register 1000 as 0.
- **Restore:** the charger remembers the last written limit. The next request after a fallback, **even a plain read, restores it** within a few seconds (observed: 1000 read 0, then 10 on the next poll, with the pilot back at 10.2 A). Reading the charger is therefore not neutral: it can resume charging at the last limit.
- **Reads keep the limit alive:** 70 s of reads only, every 3.5 s, kept the 10 A limit well past the 30 s timeout. No explicit heartbeat write is needed while the service polls.

## Design

### Architecture

Same package and process as before (`python -m charger_web`, loopback only, standard library only). The Modbus client talks to the charger; the browser talks only to the local backend; nothing here touches the MQTT setpoint server. **Modbus is the only backend the page uses.** The REST client, its login and session handling, and the session-yielding code are removed in the final task of the plan (they stay in git history); the REST design in the 2026-09-30 spec is superseded.

### The background poller (the heartbeat)

One thread owns **one persistent Modbus connection** and reads the registers above every **2 s**, whether or not a page is open. It keeps the charger's communication timeout satisfied (it must read at least once every 10 s; the timeout is 30 s) and fills a cache. HTTP handlers answer from the cache and never talk to the charger on the read path. Writes go through the same thread, so a write never interleaves with a poll.

The poller reads the 32-bit currents as two registers, high word first, and converts mA to A. A value of `0xFFFFFFFF` means "no meter" and is shown as unavailable, not as zero.

### Setting the limit (and keeping it)

The page's "Maximum current" writes **register 1000**. Allowed values are **0 A or a whole number from 6 A through `min(16, installation limit)`**, where the installation limit is register 133 when it reads, else 16. The backend validates (the browser's checks are only a convenience), writes, reads register 1000 back, and reports success only when the read-back matches.

The service keeps a **desired limit**, empty at start. After a successful Apply, the poller checks every cycle that register 1000 still equals it. If not (a charger reboot, another Modbus master), it **re-writes it once per 10 s at most** and reports a "restored" event in the status. At start the desired limit is empty and the service **never writes on its own**: it adopts whatever the charger holds. Pressing Apply with 0 is the explicit way to pause.

### What the page shows

- **Set current** (register 1000), editable.
- **Operator limit** (134), read only, labelled as the ceiling set on the AMTRON's page, with a warning when it is below the set current ("the AMTRON's own limit of 6 A caps the signalled current").
- **Signalled current** (706), **measured current per phase** (212 to 216, in A), **vehicle state** (A/B/C) and the charger's status text.
- A banner when the charger is paused (signalled 0 A with a set current above 0), and a link-state line: `ok`, `stale` (polls failing for more than 5 s, with the age of the last good reading), or `fallback expected` (no successful communication for 30 s or more, when the charger will have dropped to 0 A and restores the limit on the next request).
- A zero reading is shown as zero; a missing or refused register as unavailable.

Reads never write. Loading the page never writes. Only Apply, and the once-per-10 s restore after a charger-side change to the limit, write.

### HTTP API

`GET /api/charger/status` and `POST /api/charger/operator-limit` keep their names and the page's behaviour. The status adds `operator_limit_a` (134, read only), `set_limit_a` (1000), `installation_limit_a`, `vehicle_state`, `charger_status`, `link` and `restored_at`. The POST body is still `{"current_limit_a": n}` and sets register 1000.

### Register map file

`amtron_registers.json` becomes the documented map above: all registers `holding`, addresses as in the table, and a `size` (1 or 2 registers) and a `scale` (0.001 for the mA values). The probe tool stays as a read-only exploration aid, reading holding registers only.

### Failure behaviour

- **Charger unreachable or silent:** last good readings with their age, marked stale; no write is attempted blindly. The charger itself falls back to 0 A after 30 s without communication, which is the fail-safe.
- **A refused write or a read-back mismatch:** reported as failed with the value the charger actually holds. Nothing is repeated silently.
- **Invalid input:** rejected before any write, as in the earlier spec.
- **The service stops:** within 30 s the charger drops to 0 A. Restarting the service resumes the stored limit on its first request.

## Testing

- **A fake AMTRON** (a Modbus TCP server on localhost) with the register map above, function codes 3 and 6, the 32-bit values, a settable fallback, and the "next request restores the stored limit" behaviour. Nothing in the tests touches the real charger.
- **Unit tests:** the register decoding (32-bit, `0xFFFFFFFF`), validation (0, 6 to 16, the installation ceiling, booleans, fractions), the poller's cache and link states on a fake clock, the desired-limit and restore logic (once per 10 s, never at start), and the status shape.
- **API tests** with the fake charger: read, apply, read-back mismatch, a refused write.
- **A live check, once, with the real charger and you present:** the page reads, applies 10 A and then 0 A, shows the fallback after the service stops for more than 30 s, and works while the AMTRON's own page is open.

## Out of scope

- Authentication: Modbus TCP is unauthenticated, so enabling it exposes control of the charger to anything on the lab network. The page stays loopback-only, and the exposure is a property of the charger setting.
- Pilot-state control, the inverter and the MQTT averaging, as before.
- Starting a charge session over Modbus (Allow Start/Stop Transaction stays Off), and the charger's DLM and meter registers beyond what the page shows.
- Service supervision (starting the page automatically).

## Open points

- Register 133 is not in MENNEKES's document. It reads 16 here, but it is an inference until checked against a changed installation limit.
- The "next request restores the stored limit" behaviour was observed once and explained from the fallback description; the live check should confirm it, including after a charger restart.
- The charger's SEMP interface is on in surplus-charging mode and may also limit the signalled current below register 1000. The page shows the signalled current, so this is visible, but it is not controlled here.
