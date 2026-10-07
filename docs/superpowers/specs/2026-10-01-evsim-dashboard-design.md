# EVSim Dashboard: Inverter DC Power, Setpoints and a Live Chart — Design

**Date:** 2026-10-01 (amended 2026-10-02)
**Status:** draft, awaiting review
**Scope:** extend the local web page (`server/charger_web/`) so that, besides the AMTRON charger, it shows the **Fronius inverter's DC power**, the **board's telemetry** (`evsim/cp`), the **setpoint** the setpoint server publishes, and the **server's DC target**, with a **10-minute chart** of the four power values. The inverter's DC power and the server's DC target both come from the setpoint server's `evsim/inverter` message; the page itself never talks to the inverter. Everything added here is read-only. Builds on
[2026-10-01-charger-modbus-backend-design.md](2026-10-01-charger-modbus-backend-design.md) (the charger service, the HTTP layer) and
[2026-09-30-inverter-writer-design.md](2026-09-30-inverter-writer-design.md) (the `evsim/inverter` message).

**Amendment, 2026-10-02:** the first version of this design read the inverter directly, over a second Modbus connection (a new `InverterReader`). The `dc_w` field of `evsim/inverter` already carries the same value, MPPT 1's DC power as read once a second by the setpoint server's inverter writer, so the direct reader was dropped. That removes a Modbus client from the GEN24 and a whole unit from the page, and puts the DC power and the DC target in the same message, taken at the same moment. The cost is that the DC power exists only while the setpoint server runs with `--inverter`. The first version is in git history (commit 2d4d134).

## Why

The chain is AMTRON, rectifier, inverter, grid, with the board turning the pilot into a setpoint and the setpoint server capping the inverter to match. Whether the loop works is visible only by comparing four numbers that live in different places: the board's raw setpoint, the averaged setpoint, the DC target the server writes, and the DC power the inverter really takes. Today that needs `mosquitto_sub` and the inverter tool side by side. One page, with a chart, shows it at a glance.

A proper electric-car emulation is planned. It will replace the `evsim/setpoint` topic with an `evsim/electric_car` topic that has its own internal logic. The page therefore treats the setpoint as **one configurable source**: its topic is a setting and its payload is read by one parser, so the swap is a configuration change plus, at most, a change to that parser.

## Decisions

- The work lives **inside `charger_web`**, in the same process: it already serves the page and has the loopback HTTP layer. One new dependency, `paho-mqtt` (already in `server/requirements.txt`).
- **Read-only.** Nothing is ever published, and the page opens no connection to the inverter. The page's only Modbus peer stays the AMTRON charger, with the existing service.
- **Only the DC power** is taken from the inverter: `dc_w` in `evsim/inverter` is MPPT 1's DC power in watts, read by the setpoint server's writer once a second. The message's AC power is not used. Its `mode` is shown on the server card; `inverter_state` is carried by `/api/telemetry` but not shown.
- **Why not a direct Modbus reader:** it would be a second reader of data already on the broker, and a third Modbus client on the GEN24 (the writer, this page, any tool), which was never tested. Reading the message instead keeps the DC power and the DC target aligned for judging how well the inverter follows its target. What it gives up: an inverter view that does not depend on the setpoint server (for example while driving the inverter by hand with `tools/fronius_modbus.py`).
- The history is **in memory**, 10 minutes at 1 Hz. A restart empties it.
- Each source fails **independently**: a dead broker affects the MQTT cards and chart lines but not the charger control or its 30 s Modbus heartbeat, and the other way round.

## Design

### Units

| Unit | Job | Depends on |
|---|---|---|
| `telemetry.py` — `Telemetry` | A paho subscriber. Keeps the latest parsed value of each topic with its receive time, and the broker link state. | paho-mqtt |
| `history.py` — `History` | A 1 Hz sampler that appends one frame to a ring buffer of 600 frames. | `Telemetry` |
| `dashboard.py` — `Dashboard` | Owns the lifecycle of the two units above (the broker connection and the sampler thread) and gives `web.py` two methods: the latest readings and the history. | the two units above |
| `web.py` | Two new GET endpoints. Everything existing is unchanged. | `dashboard.py` |
| `static/` | Power-chain cards and a canvas chart. | |

`__main__` builds the server first, then starts the charger service and the dashboard (the subscriber and the sampler), so a taken port starts nothing. A `finally` stops them all.

### Subscriber

- Client id `evsim-charger-web-` plus a random suffix per process (amended 2026-10-07: a fixed id made a second page, or an old copy still running, kick this one off the broker every second; seen live). Never the setpoint server's `evsim-setpoint-server`: two clients with the same id kick each other off the broker.
- It connects asynchronously with a 1 to 5 s reconnect delay, so an absent broker never blocks start-up, and **subscribes inside `on_connect`**, so the subscriptions are renewed on every reconnect. Subscriptions are QoS 0. It has no will and publishes nothing.
- Topics: `evsim/cp`, the setpoint topic, `evsim/inverter`. The setpoint topic is `EVSIM_SETPOINT_TOPIC`, default `evsim/setpoint`; a value that is empty, longer than 128 characters or contains `+`, `#` or whitespace is refused at start-up.
- Broker: `EVSIM_MQTT_HOST` (default `127.0.0.1`), port 1883.
- **Parsers** (pure functions, never raise, drop what they cannot read):
  - `evsim/cp`: `setpoint_w` as a finite number from 0 to 12 000, plus `state` and `status`. The bounds are the setpoint server's: a corrupt value is dropped, never clamped.
  - setpoint topic: `setpoint_w` (same bounds) and `status`. This is the one function that changes when the car emulation's message differs.
  - `evsim/inverter`: `target_dc_w` (a number from 0 to 12 000, or null; anything else drops the message, as for the setpoint), `dc_w` (a finite number from 0 to 30 000 W, else `None`, and the message is kept: a measurement can exceed the 12 kW setpoint cap, and one bad number must not cost the target), `mode` and `inverter_state`.
- **Retained messages** (the setpoint topic is retained, so one arrives on every subscribe) are kept as the last known state, for example `server_offline`, but are flagged stale and never enter the history. A retained message never replaces a fresh live one. `evsim/cp` and `evsim/inverter` are not retained by their publishers; a retained copy is treated the same way.

### Readings and staleness

Every value is `{"value", "status", "updated_at"}`, as the charger readings already are. `status` is `ok` when the value is younger than 3 s, `stale` when older (or retained), and `unavailable` when there has been none or the message carried `null`. The slowest topic publishes once a second, so 3 s is two missed messages. A stale reading keeps its last value, dimmed together with the words next to it (for example "live" or "capped"); it does not turn unavailable by itself later. A `server_offline` message from the setpoint server is always stale, even when it arrives live rather than retained: it is the server's last word, not a fresh value.

### History

- The sampler runs once a second on a scheduled clock (no drift; a missed tick is skipped, not replayed). Each frame:
  `{"t": <UTC epoch seconds>, "cp_w": ..., "setpoint_w": ..., "target_dc_w": ..., "dc_w": ...}`.
- `cp_w` is the **mean of the raw `evsim/cp` `setpoint_w` samples received since the previous tick** (about five), so the raw-versus-average comparison is fair; `None` if there were none. The other three are the current reading's value if its status is `ok`, else `None`.
- The ring holds 600 frames. The window and period are constants in `config.py`.

### HTTP

Both are GET, loopback only, behind the same Host check and headers as the existing routes.

- `GET /api/telemetry` returns the latest readings:
  `{"mqtt": {"connected": bool}, "board": {"setpoint_w": reading, "state": "C", "status": "live"}, "setpoint": {"topic": "evsim/setpoint", "setpoint_w": reading, "status": "ok"}, "server": {"target_dc_w": reading, "mode": "capped", "inverter_state": 4}, "inverter": {"dc_w": reading}}`.
  `server.target_dc_w` and `inverter.dc_w` come from the same `evsim/inverter` message. Text fields are `null` when unknown.
- `GET /api/history` returns `{"interval_s": 1, "window_s": 600, "frames": [...]}`. Frames the sampler missed are absent, not filled.
- `GET /api/charger/status` and the limit write are unchanged.

### Page

- The heading changes from "AMTRON charger" to "EVSim"; the charger limit control and readings stay as the first section.
- Below it, a power-chain row of three cards: **board setpoint** (raw value, with the `live`, `held` or `zero` status and the pilot state), **server** (the setpoint topic's average and status, the DC target and `mode`) and **inverter DC power** (as read by the setpoint server). Each card shows its own stale or unavailable state.
- Below that, the chart: four lines (raw setpoint, setpoint average, DC target, inverter DC power), the last 10 minutes, a y-axis in watts from 0 to the largest value, a time axis, and a legend whose entries toggle the lines. It is a plain `<canvas>` with no library, scaled for the display's pixel ratio. A line is broken wherever a frame is missing or `None`, so an outage shows as a gap.
- The page fetches `/api/telemetry` every second and `/api/history` every two seconds, never with two requests of one kind in flight. All text is set with `textContent`; the legend toggles live in memory only.

## Error handling

| Situation | Behaviour |
|---|---|
| Broker down | The MQTT cards go stale (dimmed, last value kept); the chart shows gaps for those lines; the charger part is unaffected. The subscriber reconnects by itself. |
| Setpoint server stopped, or run without `--inverter` | No `evsim/inverter` messages: the DC target and DC power cards go stale (dimmed, last value kept) and their lines have a gap. For the setpoint topic, `server_offline` (live or retained) shows as the last known state, flagged stale. |
| Inverter unreachable by the server | The server publishes `mode: "unreachable"` with `dc_w: null`: the DC card is unavailable and the server card shows the mode. |
| Malformed message | Dropped; never an exception in a handler or a thread. A bad `dc_w` alone becomes `None`. |
| Wrong setpoint topic setting | Refused at start-up with a clear message. |

## Testing

All with the standard `unittest`, as in the existing suite.

- **Parsers**, on good, malformed, out-of-range and non-finite input, including `dc_w` (kept above 12 000 W up to 30 000 W, `None` beyond, and a bad `dc_w` keeping the target).
- **Subscriber**, against a fake client in the style of `test_server.py`: retained messages flagged stale and kept out of the history; a retained copy never overwriting a fresh value; subscription renewed on a second `on_connect`; the setpoint topic validation; the inverter card unavailable while the server reports `unreachable`.
- **History**, with a fake clock: gaps are `None`, the ring stops at 600 frames, the raw samples are averaged per second, a stale source is recorded as `None`.
- **Web and page**: both endpoints, the Host check, and static checks on the page (no `innerHTML`, a canvas, four legend entries), and the cards' rendering (value and the words beside it, stale and unavailable states) against a stub DOM in Node. The existing charger tests must still pass unchanged, apart from the one assertion on the page's heading (the heading becomes "EVSim").
- **Live check** (after the suite): the page against the real broker, with the setpoint server driving the inverter. The chart's DC line must equal `dc_w` in `evsim/inverter`, and, once, agree within a few watts with `tools/fronius_modbus.py status` (read-only), to confirm that the number the writer publishes is the inverter's own.

## Risks and open points

- The DC power and the DC target exist only while the setpoint server runs with `--inverter`. When the car emulation replaces `evsim/setpoint`, something must still publish `evsim/inverter` with `dc_w`. If the inverter writer moves out of the setpoint server, this page follows the message; if the message goes away, a direct reader (see below) is the way back.
- The page cannot show the inverter's own state, AC power or grid values.
- The car emulation's message format is unknown, so `parse_setpoint` is a guess at `setpoint_w` and `status` until that design exists.

## Not in scope

Writing to the inverter or publishing to MQTT; reading the inverter directly over Modbus (an independent view for when the setpoint server is not running: the dropped first version is in commit 2d4d134, and would be a read-only reader of SunSpec model 160 feeding the same `inverter.dc_w` reading); AC, grid, DC voltage and current values; any persistent history; authentication; the amps-to-watts conversion (`memory/evsim-amps-to-watts.md`); the car emulation itself.
