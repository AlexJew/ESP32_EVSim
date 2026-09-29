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

The client is ESP-IDF's `esp-mqtt` (`#include <mqtt_client.h>`, shipped in the Arduino-ESP32
core the project builds with; verified to link with no `platformio.ini` change). A synchronous
client such as PubSubClient was rejected: a TCP connect to an unreachable broker (a laptop that
is asleep, Mosquitto stopped) can stall `loop()` for seconds, which is the failure the WiFi
design exists to prevent.

esp-mqtt alone is not enough. Its header documents that `esp_mqtt_client_publish()` "might block
for several seconds", and `esp_mqtt_client_enqueue()`, `_start()` and `_stop()` take the same
client lock that the client's task holds while connecting. So **`loop()` never calls any
`esp_mqtt_client_*` function.** Everything that touches the client runs in two tasks — esp-mqtt's
own, and a small "pump" task pinned to core 0 that `net_mqtt_begin()` creates — and `loop()`
talks to them only through two FreeRTOS queues, always with a timeout of 0.

### Components

- **`lib/mqtt_proto`** — pure C++, natively unit-tested. `mqtt_rx_filter()` decides whether an
  incoming message may become a command (not retained, non-empty, a single unfragmented chunk
  shorter than 16 bytes, no NUL byte); `mqtt_sanitize()` copies untrusted bytes with everything
  outside printable ASCII replaced by `?`, for echoing on serial. Payload *parsing* is not
  duplicated: `pilot_parse_command()` is reused, so serial and MQTT accept identical text.
- **`src/net_mqtt.{h,cpp}`** — wraps esp-mqtt. Interface: `net_mqtt_begin()`,
  `net_mqtt_take_command()`, `net_mqtt_publish_state()`, `net_mqtt_publish_reply()`,
  `net_mqtt_print_status()`.
- **`src/mqtt_config.h`** — broker URI (an IP, never a hostname), client ID, the four topic
  strings, keepalive and reconnect timing. Same pattern as `wifi_config.h`.
- **`src/main.cpp`** — remains the only code that touches `g_state` and the GPIOs.

### Data flow

1. The esp-mqtt task receives a message on `evsim/cmd/state`, runs `mqtt_rx_filter()`, and on
   `Accept` copies the payload into the RX queue (depth 4, 16 bytes each, `xQueueSend` timeout 0;
   a full queue drops the message and counts it). It does nothing else.
2. `loop()` calls `net_mqtt_take_command()` once per pass (`xQueueReceive`, timeout 0) and passes
   any command to the existing `handle_command()`.
3. `handle_command()` builds each reply as one string and sends it through one function: it
   always prints the `# ...` line on serial, and for a command that came from MQTT it also calls
   `net_mqtt_publish_reply()`. `W` (WiFi/MQTT status) is serial-only; over MQTT it is an unknown
   command.
4. `net_mqtt_publish_state()` and `net_mqtt_publish_reply()` copy the text into the TX queue
   (depth 8, 64 bytes each, timeout 0). A successful state change calls the former whatever the
   command's source.
5. The pump task drains the TX queue into `esp_mqtt_client_enqueue()` — only while the client is
   connected; otherwise the item is dropped, because the connect handler republishes the current
   state anyway and a stale reply would be misleading.

### Connection handling

The pump task also owns the client's lifecycle. Every 100 ms it reads `WiFi.status()`: WiFi up
and client not started → `esp_mqtt_client_start()`; WiFi down and client started →
`esp_mqtt_client_stop()`. esp-mqtt's built-in auto-reconnect stays **enabled** (5 s): the client
only runs while WiFi is up, so its retry toward a dead broker is harmless and needs no policy
code. (The WiFi SDK's reconnect engine had to be disabled because it raced the project's own
policy; there is no such policy here.)

On the `CONNECTED` event the esp-mqtt task subscribes to `evsim/cmd/state` and publishes `online`
and the current state. The last-will is `offline` on `evsim/status`, retained; the keepalive is
10 s so the broker publishes it about 15 s after the board vanishes.

With the broker down the board keeps its state and keeps sampling CP.

## Testing

- **Native unit tests** (`pio test -e native`): message-acceptance rules (retained, empty,
  oversize, fragmented and NUL-containing payloads are ignored) and sanitizing of echoed bytes.
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
  200 ms sample tick through the same TX queue, with rate limiting so publishing cannot disturb
  sampling.
- Authentication and TLS. Revisit when the separate server takes over and the broker leaves the
  computer; note that the broker IP in `mqtt_config.h` must then change.
- The REST client for the inverter.
