---
name: evsim-setpoint-server
description: EVSim setpoint server (server/) - averages evsim/cp setpoint_w over 1 s and publishes JSON on evsim/setpoint for the Fronius; runs on the dev computer
metadata:
  type: project
---

A Python server in `server/` on the development computer (192.168.21.137). It subscribes to the
board's `evsim/cp`, takes the mean of `setpoint_w` over the last 1 s, and publishes
`{"setpoint_w":...,"samples":...,"window_s":1,"status":"ok|no_data|server_offline","ts":...}` on
`evsim/setpoint` (QoS 0, retained: QoS 1 would replay a stale setpoint after a reconnect) once per second. No data in the window means 0 W, never the last
value. A `setpoint_w` above 12 000 W (the inverter's rating) is dropped as a corrupt message, not
clamped. Design: `docs/superpowers/specs/2026-09-30-setpoint-server-design.md`.

**Why:** the board publishes raw samples and leaves smoothing to the consumer
([[evsim-tb-overview]]); this server is that consumer, in the "separate server" form.

**How to apply:** it only puts a setpoint on the broker. Whether the GEN24 needs an MQTT -> Modbus
bridge to act on it is still open ([[evsim-inverter-fronius]]). The averaging is a plain 1 s mean by
design; anything smoother is a later change here, not on the board. Run it with
`cd server && .venv/bin/python -m setpoint_server`; see `server/README.md`.

**The cap was raised from 11 000 to 12 000 W (2026-09-30):** the board's watts come from amps x 690 V, so the
charger's 16 A limit reads about 11 190 W, which the 11 kW cap dropped as corrupt (the server saw no data).
12 000 W is the inverter's rating and leaves room for a full 16 A offer.

## Deferred review findings (2026-09-30)

Minor issues from the final review that were not fixed. None affects the bench setup today; fix them before
the server is relied on unattended.

- **Mac sleep:** `time.monotonic` (macOS `mach_absolute_time`) stops during sleep, so samples from just before
  sleep are still "in the window" on wake. The first tick can publish that stale `ok` with a fresh `ts`.
  Fix: `time.clock_gettime(time.CLOCK_MONOTONIC)` on macOS (counts sleep). Matters if the computer running the
  broker and this server ever sleeps.
- **Two server copies:** they do not take over once, as the spec says. Each reconnect kicks the other off,
  about once a second, so `evsim/setpoint` flickers between the value and 0 W (safe direction, but noisy).
  Fix: correct the spec wording, or exit when the session is taken over.
- **Signal handler:** it calls `Event.set()`, which can deadlock if SIGINT/SIGTERM lands while the main thread
  holds the Event's lock inside `wait()`. Tiny chance; only SIGKILL recovers. Fix: set a plain flag, or use a
  helper thread.
- **Shutdown log:** `shutdown()` logs "published server_offline" even when `wait_for_publish` timed out. Fix:
  log from `info.is_published()`.
- **Test gaps:** only SIGTERM is benched (not Ctrl-C), and stopping while the broker is down is not benched.
- **Outage logging:** no `on_connect_fail` handler, so the reason a broker is unreachable never reaches the log,
  and an outage logs one warning per second.

Also not seen on hardware yet: the held setpoint through the charger's 6-12 s gaps (the bench run was 8 s).

## Driving the inverter (added 2026-09-30)

With `--inverter HOST` the server also caps the Fronius inverter over Modbus TCP so that its DC input from the
charger equals the setpoint; see [[evsim-inverter-fronius]] and
`docs/superpowers/specs/2026-09-30-inverter-writer-design.md`. Dead-band 100 W, refresh 10 s, 0 % cap on no
data, standby after 120 s, 0 % cap left on a clean stop. Status on `evsim/inverter`. Off unless the flag is given.
