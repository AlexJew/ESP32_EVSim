# MQTT Pilot-State Commands — Design

**Date:** 2026-09-29
**Status:** awaiting review
**Scope:** let the board take pilot-state commands (A, B, C) from an MQTT broker on the LAN and
report the state it presents and the outcome of each command. Publishing CP measurements
(duty, current, power) is a separate, later design; the topic prefix leaves room for it.

## Purpose

Today the pilot state is commanded over serial only. The thesis loop needs it commanded over
the network. Stage one: a human publishes with `mosquitto_pub` from a computer that also runs
the broker. Stage two: a separate server publishes to the same topic, with the broker moved off
the computer. The firmware must not change between stages, so the topic contract below is the
interface, and nothing in the firmware may depend on who publishes.

The board depends on the static-IP WiFi work
([2026-09-25-wifi-static-ip-design.md](2026-09-25-wifi-static-ip-design.md)) and inherits its
constraints: address the broker **by IP** (DNS is unconfirmed and there is no internet route),
and never let a network condition stop, delay or corrupt CP measurement.

## Topic contract

Prefix `evsim/`. All payloads are plain ASCII, no JSON.

| Topic | Direction | Payload | QoS | Retained |
|---|---|---|---|---|
| `evsim/cmd/state` | broker → board | `A`, `B` or `C` (case and surrounding whitespace ignored, exactly as on serial) | 1 | no |
| `evsim/state` | board → broker | the state actually presented: `A`, `B` or `C` | 1 | yes |
| `evsim/reply` | board → broker | one line: `state A -> B`, `already in B`, `refused A -> C: state B must be asserted first`, `unknown command 'X'` | 1 | no |
| `evsim/status` | board → broker | `online`, or `offline` (the last-will) | 1 | yes |

### Rules

1. **Retained commands are ignored.** A command is an event, not a setting. A retained `C`
   would otherwise be replayed at every reconnect and change what the charger sees.
2. **Clean session.** Commands published while the board was offline are never delivered later.
3. **Losing the broker or WiFi holds the current state.** The charger keeps seeing what it saw;
   only a command or a reset changes it. Dropping to A on disconnect would look to the charger
   like the car being unplugged. This follows the "hold the last valid setpoint" constraint in
   `memory/evsim-open-questions.md`.
4. **On every (re)connect** the board publishes `online` on `evsim/status` and its current state
   on `evsim/state`, so a late subscriber learns the state from the retained message.
5. **Every command gets a reply** on `evsim/reply`, accepted or refused. Serial keeps working
   alongside MQTT and both go through the same validation, so A → C is refused on both.
6. The state after reset is always A, as today.

## Design

### Client: ESP-IDF `esp-mqtt`

The client is ESP-IDF's `esp-mqtt` (`mqtt_client.h`, shipped in the Arduino-ESP32 core the
project already builds with; verified present for the `esp32` target). It runs in its own
FreeRTOS task, so connecting, reconnecting and publishing never run on `loop()`. A synchronous
client such as PubSubClient was rejected: a TCP connect to an unreachable broker (a laptop that
is asleep, Mosquitto stopped) can stall `loop()` for seconds, which is the failure the WiFi
design exists to prevent.

### Components

- **`lib/mqtt_proto`** — pure C++, natively unit-tested. Topic names built from the prefix;
  payload parsing, reusing `pilot_parse_command()` so serial and MQTT accept identical text;
  the rule that a retained message is not a command.
- **`src/net_mqtt.{h,cpp}`** — wraps esp-mqtt, alongside `net_wifi`. Interface:
  `net_mqtt_begin()`, `net_mqtt_poll()`, `net_mqtt_publish_state()`,
  `net_mqtt_publish_reply()`.
- **`src/mqtt_config.h`** — broker IP (never a hostname), port, prefix, client ID. Same pattern
  as `wifi_config.h`.
- **`src/main.cpp`** — remains the only code that touches `g_state` and the GPIOs.

### Data flow

1. The esp-mqtt task receives a message on `evsim/cmd/state`, drops it if retained, and pushes
   the payload (at most 16 characters, like `kLineMax`) into a small FreeRTOS queue. It does
   nothing else.
2. `loop()` calls `net_mqtt_poll()`, which takes **at most one** command per pass — as
   `poll_serial()` does — and calls the existing `handle_command()`.
3. `handle_command()`'s replies go through one function. It always prints the `# ...` line on
   serial, and also publishes on `evsim/reply` when the command came from MQTT.
4. A successful state change publishes `evsim/state` whatever its source, so a serial command
   is visible on the broker too.

### Connection handling

`net_mqtt_poll()` starts the client when WiFi reports connected and stops it when WiFi drops.
esp-mqtt's built-in auto-reconnect stays **enabled**: the client only runs while WiFi is up, so
its retry toward a dead broker is harmless, runs in its own task and needs no policy code. (The
WiFi SDK's reconnect engine had to be disabled because it raced the project's own policy; there
is no such policy here.)

With the broker down the board keeps its state and keeps sampling CP. If the command queue is
full the command is dropped and a line goes to serial only; this cannot happen at human speed.

The last-will is `offline` on `evsim/status`, retained.

## Testing

- **Native unit tests** (`pio test -e native`): topic building, payload parsing including the
  serial-equivalent leniency, retained-message rejection.
- **CP-sampling interference**: run `tools/cp_jitter.py` with MQTT connected, and again with
  the broker stopped so the client retries continuously; `compare` shows whether MQTT disturbs
  CP sampling. The baseline is the firmware without MQTT, as for WiFi.
- **Bench, with `mosquitto_pub` / `mosquitto_sub`**: A → B → C accepted; A → C refused with a
  reply; a retained command ignored; broker restart, state held and republished on reconnect;
  WiFi drop while in state C, state held.
- Both firmware environments (`esp32dev`, `esp32dev-lowvoltage`) must build.

## Broker setup (stage one)

Mosquitto 2.x listens on localhost only by default. The computer's broker needs a config with
`listener 1883` and `allow_anonymous true` for the board to reach it. That is acceptable on the
laboratory LAN for stage one.

## Out of scope

- Publishing CP measurements (`evsim/cp`), the next design. It will publish from the existing
  200 ms sample tick into the same client, with rate limiting so publishing cannot disturb
  sampling.
- Authentication and TLS. Revisit when the separate server takes over and the broker leaves the
  computer; note that the broker IP in `mqtt_config.h` must then change.
- The REST client for the inverter.
