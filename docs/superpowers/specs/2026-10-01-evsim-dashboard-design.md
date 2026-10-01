# EVSim Dashboard: Inverter DC Power, Setpoints and a Live Chart — Design

**Date:** 2026-10-01
**Status:** draft, awaiting review
**Scope:** extend the local web page (`server/charger_web/`) so that, besides the AMTRON charger, it shows the **Fronius inverter's DC power** (Modbus TCP), the **board's telemetry** (`evsim/cp`), the **setpoint** the setpoint server publishes, and the **server's DC target** (`evsim/inverter`), with a **10-minute chart** of the four power values. Everything added here is read-only. Builds on
[2026-10-01-charger-modbus-backend-design.md](2026-10-01-charger-modbus-backend-design.md) (the charger service, the HTTP layer) and
[2026-09-30-inverter-writer-design.md](2026-09-30-inverter-writer-design.md) (the `evsim/inverter` message).

## Why

The chain is AMTRON, rectifier, inverter, grid, with the board turning the pilot into a setpoint and the setpoint server capping the inverter to match. Whether the loop works is visible only by comparing four numbers that live in different places: the board's raw setpoint, the averaged setpoint, the DC target the server writes, and the DC power the inverter really takes. Today that needs `mosquitto_sub` and the inverter tool side by side. One page, with a chart, shows it at a glance.

A proper electric-car emulation is planned. It will replace the `evsim/setpoint` topic with an `evsim/electric_car` topic that has its own internal logic. The page therefore treats the setpoint as **one configurable source**: its topic is a setting and its payload is read by one parser, so the swap is a configuration change plus, at most, a change to that parser.

## Decisions

- The work lives **inside `charger_web`**, in the same process: it already serves the page and has a hardened Modbus client. One new dependency, `paho-mqtt` (already in `server/requirements.txt`).
- **Read-only.** The inverter is only ever read (function code 3) and nothing is ever published. No new write path exists.
- **Only the DC power** is taken from the inverter: MPPT 1's `DCW`, from SunSpec model 160, once a second. No AC power, grid values or inverter state (model 103); the state still arrives with the server's `evsim/inverter` message when the server runs with `--inverter`. The same read holds the DC voltage and current; they are left out until wanted.
- The history is **in memory**, 10 minutes at 1 Hz. A restart empties it.
- Each source fails **independently**: a dead broker or an unreachable inverter affects only its own cards and chart lines. The charger control and its 30 s Modbus heartbeat are untouched.

## Design

### Units

| Unit | Job | Depends on |
|---|---|---|
| `inverter.py` — `InverterReader` | One thread and one persistent connection to the inverter. Once a second it reads model 160 in one request and caches MPPT 1's DC power. Exposes reads only. | `modbus.py` |
| `telemetry.py` — `Telemetry` | A paho subscriber. Keeps the latest parsed value of each topic with its receive time, and the broker link state. | paho-mqtt |
| `history.py` — `History` | A 1 Hz sampler that appends one frame to a ring buffer of 600 frames. | the two units above |
| `web.py` | Two new GET endpoints. Everything existing is unchanged. | |
| `static/` | Power-chain cards and a canvas chart. | |

`__main__` builds the server first, then starts the charger service, the inverter reader, the subscriber and the sampler, so a taken port starts nothing. A `finally` stops them all.

### Inverter reader

- The SunSpec chain is walked **once per connection**, only to find model 160's first address and length (marker `SunS` at 40000; every model's header gives its id and length). It is re-walked after a reconnect.
- Each poll reads the whole of model 160 in one request, so the scale factors and the values come from the same instant. (On 2026-09-30 the DC power read 10× too large during start-up because a scale factor changed between two reads.)
- MPPT 1 is the module whose **id is 1**, not the first in the list. `dc_w = DCW × 10^DCW_SF`. A raw value of 65535 ("not implemented") gives `None`.
- Any transport error closes the connection and is retried with a back-off of 1 s up to 5 s. The last good value simply ages and turns stale. A malformed frame is a transport error (the client already enforces this).
- The default host is `192.168.21.120`, set by `EVSIM_INVERTER_HOST` (validated like `EVSIM_CHARGER_HOST`). Unit ID 1, port 502.

### Subscriber

- Client id `evsim-charger-web`. It must differ from the setpoint server's `evsim-setpoint-server`: two clients with the same id kick each other off the broker.
- It connects asynchronously with a 1 to 5 s reconnect delay, so an absent broker never blocks start-up, and **subscribes inside `on_connect`**, so the subscriptions are renewed on every reconnect. Subscriptions are QoS 0. It has no will and publishes nothing.
- Topics: `evsim/cp`, the setpoint topic, `evsim/inverter`. The setpoint topic is `EVSIM_SETPOINT_TOPIC`, default `evsim/setpoint`; a value that is empty, longer than 128 characters or contains `+`, `#` or whitespace is refused at start-up.
- Broker: `EVSIM_MQTT_HOST` (default `127.0.0.1`), port 1883.
- **Parsers** (pure functions, never raise, drop what they cannot read):
  - `evsim/cp`: `setpoint_w` as a finite number from 0 to 12 000, plus `state` and `status`. The bounds are the setpoint server's: a corrupt value is dropped, never clamped.
  - setpoint topic: `setpoint_w` (same bounds) and `status`. This is the one function that changes when the car emulation's message differs.
  - `evsim/inverter`: `target_dc_w` (a number, or null), `mode`, `inverter_state`.
- **Retained messages** (the setpoint topic is retained, so one arrives on every subscribe) are kept as the last known state, for example `server_offline`, but are flagged stale and never enter the history. `evsim/cp` and `evsim/inverter` are not retained by their publishers; a retained copy is treated the same way.

### Readings and staleness

Every value is `{"value", "status", "updated_at"}`, as the charger readings already are. `status` is `ok` when the value is younger than the limit, `stale` when older (or retained), and `unavailable` when there has been none. The limits are 3 s for the MQTT values and the existing `STALE_AFTER_S` (5 s) for the inverter. The inverter reading is also `stale` while its link is down.

### History

- The sampler runs once a second on a scheduled clock (no drift; a missed tick is skipped, not replayed). Each frame:
  `{"t": <UTC epoch seconds>, "cp_w": ..., "setpoint_w": ..., "target_dc_w": ..., "dc_w": ...}`.
- `cp_w` is the **mean of the raw `evsim/cp` `setpoint_w` samples received since the previous tick** (about five), so the raw-versus-average comparison is fair; `None` if there were none. The other three are the current reading's value if its status is `ok`, else `None`.
- The ring holds 600 frames. The window and period are constants in `config.py`.

### HTTP

Both are GET, loopback only, behind the same Host check and headers as the existing routes.

- `GET /api/telemetry` returns the latest readings:
  `{"mqtt": {"connected": bool}, "board": {"setpoint_w": reading, "state": "C", "status": "live"}, "setpoint": {"topic": "evsim/setpoint", "setpoint_w": reading, "status": "ok"}, "server": {"target_dc_w": reading, "mode": "capped", "inverter_state": 4}, "inverter": {"dc_w": reading}}`.
  Text fields are `null` when unknown.
- `GET /api/history` returns `{"interval_s": 1, "window_s": 600, "frames": [...]}`. Frames the sampler missed are absent, not filled.
- `GET /api/charger/status` and the limit write are unchanged.

### Page

- The heading changes from "AMTRON charger" to "EVSim"; the charger limit control and readings stay as the first section.
- Below it, a power-chain row of three cards: **board setpoint** (raw value, with the `live`, `held` or `zero` status and the pilot state), **server** (the setpoint topic's average and status, the DC target and `mode`) and **inverter DC power**. Each card shows its own stale or unavailable state.
- Below that, the chart: four lines (raw setpoint, setpoint average, DC target, inverter DC power), the last 10 minutes, a y-axis in watts from 0 to the largest value, a time axis, and a legend whose entries toggle the lines. It is a plain `<canvas>` with no library, scaled for the display's pixel ratio. A line is broken wherever a frame is missing or `None`, so an outage shows as a gap.
- The page fetches `/api/telemetry` every second and `/api/history` every two seconds, never with two requests of one kind in flight. All text is set with `textContent`; the legend toggles live in memory only.

## Error handling

| Situation | Behaviour |
|---|---|
| Broker down | The cards from MQTT go stale, then unavailable; the chart shows gaps for those lines; the inverter and charger parts are unaffected. The subscriber reconnects by itself. |
| Inverter unreachable | The DC card goes stale, then unavailable; the DC line has a gap. The reader retries with back-off. |
| Setpoint server stopped | The retained `server_offline` shows as the last known state, flagged stale. |
| Malformed message or register | Dropped (MQTT) or a transport error (Modbus); never an exception in a handler or a thread. |
| Wrong setpoint topic setting | Refused at start-up with a clear message. |

## Testing

All with the standard `unittest`, as in the existing suite.

- **Inverter reader**, against the existing `FakeInverter`: decodes the scale factors; takes the module whose id is 1; gives `None` for 65535; reconnects and re-walks the chain after a dropped connection; never writes (the fake records every write, and the test asserts the list stays empty).
- **Subscriber**, against a fake client in the style of `test_server.py`: each parser on good, malformed, out-of-range and non-finite input; retained messages flagged stale and kept out of the history; subscription renewed on a second `on_connect`; the setpoint topic validation.
- **History**, with a fake clock: gaps are `None`, the ring stops at 600 frames, the raw samples are averaged per second, a stale source is recorded as `None`.
- **Web and page**: both endpoints, the Host check, and static checks on the page (no `innerHTML`, a canvas, four legend entries). The existing charger tests must still pass unchanged.
- **Live check** (after the suite): the page against the real broker and inverter, with the setpoint server driving the inverter at the same time. The chart's DC line must agree with `evsim/inverter` `dc_w`.

## Risks and open points

- The GEN24 now has up to three Modbus clients (the setpoint server's writer, this page, any tool). Two at once worked on 2026-10-01; three are untested. The reader isolates its failures, so the worst case is a missing DC line. The live check runs all three.
- Model 160 is read, not model 103, so the page cannot show the inverter's own state when the setpoint server runs without `--inverter`.
- The car emulation's message format is unknown, so `parse_setpoint` is a guess at `setpoint_w` and `status` until that design exists.

## Not in scope

Writing to the inverter or publishing to MQTT; AC, grid, DC voltage and current values; any persistent history; authentication; the amps-to-watts conversion (`memory/evsim-amps-to-watts.md`); the car emulation itself.
