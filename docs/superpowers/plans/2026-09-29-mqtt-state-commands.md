# MQTT Pilot-State Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the EVSim ESP32 take pilot-state commands (`A`, `B`, `C`) from an MQTT broker on the LAN, and report the state it presents and the outcome of every command, without ever letting the network delay CP sampling.

**Architecture:** The message-acceptance rules live in a pure library (`lib/mqtt_proto`), unit-tested on the Mac. `src/net_mqtt.*` wraps ESP-IDF's `esp-mqtt` client and confines **every** call into it to two FreeRTOS tasks — esp-mqtt's own task and one small "pump" task — because esp-mqtt's publish/enqueue calls can block for seconds. `loop()` only touches two zero-timeout queues. `main.cpp` keeps sole ownership of `g_state` and the GPIOs; its one command handler now serves serial and MQTT alike.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev`, Unity tests on the `native` env, `esp-mqtt` from the Arduino-ESP32 core (`#include <mqtt_client.h>`; verified to link with no `platformio.ini` change). Mosquitto (Homebrew, already installed) as the broker. `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. `pio` commands run from the `EVSim/` directory; `git` commands run from the repository root, with repo-relative paths (`EVSim/src/...`).

**Spec:** `docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md` (Task 1 amends it first: the design as written let `loop()` call esp-mqtt directly).

## Global Constraints

- Topics, exact: `evsim/cmd/state` (subscribe, QoS 1), `evsim/state` (publish, QoS 1, retained), `evsim/reply` (publish, QoS 1, not retained), `evsim/status` (publish, QoS 1, retained; last-will payload `offline`, online payload `online`).
- Command payloads: `A`, `B` or `C`, case and surrounding whitespace ignored — the same text `pilot_parse_command()` accepts on serial. The A → C refusal applies identically to both channels.
- **Retained command messages are ignored.** Clean session. No command published while the board was offline is ever delivered later.
- **Losing WiFi or the broker holds the current pilot state.** Nothing in the firmware may change `g_state` or the GPIOs because of a connection event.
- On every (re)connect the board publishes `online` on `evsim/status` and its current state on `evsim/state`.
- **No network condition may delay CP sampling.** `loop()` never calls into `esp_mqtt_client_*`, never waits on a lock, and only uses `xQueueSend`/`xQueueReceive` with a **timeout of 0**. At most one MQTT command is handled per `loop()` pass.
- The broker is addressed **by IP** (`192.168.21.137`, the computer), never by hostname; DNS is unconfirmed and there is no internet route.
- esp-mqtt's auto-reconnect stays **enabled** (`reconnect_timeout_ms = 5000`); the client is only started while `WiFi.status() == WL_CONNECTED`.
- Keepalive is 10 s, so the broker publishes the last-will about 15 s after the board vanishes.
- `lib/mqtt_proto/` must **not** include `Arduino.h` or any Arduino/FreeRTOS type — it builds for `[env:native]`.
- **The CSV schema does not change**; all MQTT diagnostics are `#`-prefixed lines, and none are printed from a task other than `loop()`'s.
- **Do not modify** `lib/cp_convert/`, `lib/pilot_state/`, `lib/net_state/`, `lib/cp_adc_level/`, `src/cp_reader_*`, `src/pilot_driver.*`, or `src/net_wifi.*`. Edited files: `src/main.cpp`, and the spec.
- The default pilot-state behaviour and every existing serial reply text stay byte-identical (`# state A -> B`, `# already in B`, `# refused A -> C: state B must be asserted first`, `# unknown command 'X'`), except that non-printable bytes in an echoed unknown command are shown as `?`.
- Never `git add -A` or `git add .`; `EVSim/.vscode/c_cpp_properties.json`, `Data_analysis_PWM.xlsx` and `data/` may carry unrelated local changes. Add only the files named in each task.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Most likely first. Each line has a test in the task that owns the code (native where the logic is pure, bench otherwise).

1. **A retained command replayed at reconnect** must not change the state (a stale retained `C` would otherwise re-fire on every reconnect). Pinned by Task 2 (retained rule) and Task 5 (bench: retained publish, reset the board, state stays A).
2. **Empty, oversize, fragmented or NUL-containing payloads** (`mosquitto_pub -n`, a 20-character message, `A\0x`) must be ignored — never crash, never become a command from a truncated prefix. Task 2 (native) and Task 5.
3. **Control characters in a payload** (`B\n` from `mosquitto_pub -s`; a stray `\r`, tab, or byte ≥ 0x80 in an unknown command) must neither be refused when it is a valid command nor corrupt the `#` line or CSV on serial. Task 2 (sanitize, native) and Task 5.
4. **Broker down, then back, while a state was commanded** — the state is held, CP sampling continues, and the retained `evsim/state` equals the *actual* state afterwards (no stale value replayed from the client's outbox). Task 5.
5. **A burst of commands** (10 published back to back) never blocks `loop()` or the CSV; excess beyond the 4-deep queue is dropped and counted, not fatal. Task 5.

---

## File Structure

| File | Responsibility |
|---|---|
| `EVSim/lib/mqtt_proto/mqtt_proto.h` / `.cpp` | Create. Pure: whether an incoming message may become a command (`mqtt_rx_filter`), and safe echoing of untrusted bytes (`mqtt_sanitize`). |
| `EVSim/test/test_mqtt_proto/test_mqtt_proto.cpp` | Create. Host tests for the above. |
| `EVSim/src/mqtt_config.h` | Create. Broker URI, client ID, topics, timing constants. |
| `EVSim/src/net_mqtt.h` / `.cpp` | Create. esp-mqtt wrapper: two queues, the pump task, the event handler. |
| `EVSim/src/main.cpp` | Modify. Source-aware `handle_command`, one reply function, `poll_mqtt`, state publishing. |
| `tools/mosquitto-lab.conf` | Create. Broker config that listens on the LAN. |
| `docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md` | Modify (Task 1). |

---

### Task 1: Amend the spec — `loop()` must never call esp-mqtt

**Why:** the header documents that `esp_mqtt_client_publish()` "might block for several seconds" (network timeout 10 s), and `esp_mqtt_client_enqueue()`/`start`/`stop` share the client's lock with a task that may be inside a connect attempt to a dead broker. The spec said `loop()` would call `net_mqtt_poll()` and `net_mqtt_publish_*` directly; that would let a stopped Mosquitto stall CP sampling — the exact failure the design exists to prevent.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md`

**Interfaces:**
- Produces: the design that Tasks 2–5 implement (`net_mqtt_begin`, `net_mqtt_take_command`, `net_mqtt_publish_state`, `net_mqtt_publish_reply`, `net_mqtt_print_status`; literal topic constants instead of a prefix).

- [ ] **Step 1: Replace the "Client", "Components", "Data flow" and "Connection handling" subsections**

In the spec, replace everything from `### Client: ESP-IDF esp-mqtt` up to (not including) `## Testing` with:

```markdown
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

```
- [ ] **Step 2: Update the "Components" wording elsewhere in the spec**

In the spec's "Topic contract" heading area nothing changes, but the sentence in **Out of scope** that reads "It will publish from the existing 200 ms sample tick into the same client" must become: "It will publish from the existing 200 ms sample tick through the same TX queue, with rate limiting so publishing cannot disturb sampling." Make that one edit.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md
git commit -m "Spec: keep loop() out of esp-mqtt, whose publish can block for seconds

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `lib/mqtt_proto` — the pure acceptance rules

**Files:**
- Create: `EVSim/lib/mqtt_proto/mqtt_proto.h`
- Create: `EVSim/lib/mqtt_proto/mqtt_proto.cpp`
- Test: `EVSim/test/test_mqtt_proto/test_mqtt_proto.cpp`

**Interfaces:**
- Produces (used by Tasks 3 and 4):
  - `constexpr size_t kMqttCommandCapacity = 16;`
  - `enum class MqttRx { Accept, IgnoreRetained, IgnoreEmpty, IgnoreMalformed };`
  - `MqttRx mqtt_rx_filter(bool retained, int data_len, int total_data_len, int current_offset, bool has_nul);`
  - `void mqtt_sanitize(const char *src, char *dst, size_t dst_size);`

- [ ] **Step 1: Write the failing test**

Create `test/test_mqtt_proto/test_mqtt_proto.cpp`:

```cpp
// Host-side tests for the MQTT message acceptance rules. Run with:
//   pio test -e native
#include <string.h>
#include <unity.h>

#include "mqtt_proto.h"

// --- mqtt_rx_filter --------------------------------------------------------

void test_a_plain_single_chunk_command_is_accepted(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::Accept,
                        (int)mqtt_rx_filter(false, 1, 1, 0, false));
}

void test_a_retained_message_is_ignored(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreRetained,
                        (int)mqtt_rx_filter(true, 1, 1, 0, false));
}

void test_retained_wins_over_every_other_defect(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreRetained,
                        (int)mqtt_rx_filter(true, 0, 0, 0, true));
}

void test_an_empty_payload_is_ignored(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreEmpty,
                        (int)mqtt_rx_filter(false, 0, 0, 0, false));
}

void test_the_longest_accepted_payload_is_capacity_minus_one(void) {
  const int longest = (int)kMqttCommandCapacity - 1;
  TEST_ASSERT_EQUAL_INT((int)MqttRx::Accept,
                        (int)mqtt_rx_filter(false, longest, longest, 0, false));
  TEST_ASSERT_EQUAL_INT(
      (int)MqttRx::IgnoreMalformed,
      (int)mqtt_rx_filter(false, longest + 1, longest + 1, 0, false));
}

void test_a_fragmented_message_is_ignored(void) {
  // First chunk of a 20-byte message: data_len < total_data_len.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 5, 20, 0, false));
  // A later chunk: non-zero offset.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 5, 20, 5, false));
}

void test_a_payload_containing_a_nul_byte_is_ignored(void) {
  // "A\0x" would otherwise be parsed as the command "A" from its prefix.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 3, 3, 0, true));
}

// --- mqtt_sanitize ---------------------------------------------------------

void test_sanitize_keeps_printable_ascii(void) {
  char out[16];
  mqtt_sanitize("Ab 9~", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("Ab 9~", out);
}

void test_sanitize_replaces_a_trailing_newline(void) {
  char out[16];
  mqtt_sanitize("A\n", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("A?", out);
}

void test_sanitize_replaces_control_and_high_bytes(void) {
  char out[16];
  mqtt_sanitize("\t\x01\x7f\xff\r", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("?????", out);
}

void test_sanitize_truncates_to_the_destination_and_terminates(void) {
  char out[4];
  mqtt_sanitize("ABCDEF", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("ABC", out);
}

void test_sanitize_into_a_one_byte_buffer_yields_the_empty_string(void) {
  char out[1] = {'x'};
  mqtt_sanitize("ABC", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("", out);
}

void test_sanitize_of_null_yields_the_empty_string(void) {
  char out[4] = {'x', 'x', 'x', 'x'};
  mqtt_sanitize(nullptr, out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("", out);
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_plain_single_chunk_command_is_accepted);
  RUN_TEST(test_a_retained_message_is_ignored);
  RUN_TEST(test_retained_wins_over_every_other_defect);
  RUN_TEST(test_an_empty_payload_is_ignored);
  RUN_TEST(test_the_longest_accepted_payload_is_capacity_minus_one);
  RUN_TEST(test_a_fragmented_message_is_ignored);
  RUN_TEST(test_a_payload_containing_a_nul_byte_is_ignored);
  RUN_TEST(test_sanitize_keeps_printable_ascii);
  RUN_TEST(test_sanitize_replaces_a_trailing_newline);
  RUN_TEST(test_sanitize_replaces_control_and_high_bytes);
  RUN_TEST(test_sanitize_truncates_to_the_destination_and_terminates);
  RUN_TEST(test_sanitize_into_a_one_byte_buffer_yields_the_empty_string);
  RUN_TEST(test_sanitize_of_null_yields_the_empty_string);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `~/.platformio/penv/bin/pio test -e native -f test_mqtt_proto`
Expected: FAIL to build — `mqtt_proto.h: No such file or directory`.

- [ ] **Step 3: Write the header**

Create `lib/mqtt_proto/mqtt_proto.h`:

```cpp
// Rules for an incoming MQTT message that might become a pilot-state command,
// and for echoing untrusted bytes safely.
//
// Pure logic with no Arduino or FreeRTOS dependency, so it builds and is tested
// on the host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md.
#pragma once

#include <stddef.h>

// Longest command text the board accepts, terminator included. Equal to the
// serial line buffer in main.cpp, so both channels take the same input.
constexpr size_t kMqttCommandCapacity = 16;

enum class MqttRx {
  Accept,           // may become a command
  IgnoreRetained,   // a retained message is a stored event, not a live command
  IgnoreEmpty,      // no payload
  IgnoreMalformed,  // fragmented, too long, or contains a NUL byte
};

// Decides whether a message received on the command topic may become a
// command. The arguments are the fields of esp-mqtt's DATA event.
//
// A command is an event, not a setting: a retained message would be replayed
// at every reconnect and change the state the charger sees, so it is never
// accepted. Anything that is not a single, complete, NUL-free chunk shorter
// than kMqttCommandCapacity is refused rather than truncated, because a
// truncated prefix could parse as a valid command the publisher never sent.
// `retained` is checked first.
MqttRx mqtt_rx_filter(bool retained, int data_len, int total_data_len,
                      int current_offset, bool has_nul);

// Copies `src` into `dst`, replacing every byte outside printable ASCII
// (0x20..0x7E) with '?', so untrusted input can be echoed on a '#' line without
// breaking the line or the CSV. Always terminates `dst`; truncates to fit. A
// null `src` or a zero-size `dst` is handled (the latter is left untouched).
void mqtt_sanitize(const char *src, char *dst, size_t dst_size);
```

- [ ] **Step 4: Write the implementation**

Create `lib/mqtt_proto/mqtt_proto.cpp`:

```cpp
#include "mqtt_proto.h"

MqttRx mqtt_rx_filter(bool retained, int data_len, int total_data_len,
                      int current_offset, bool has_nul) {
  if (retained) {
    return MqttRx::IgnoreRetained;
  }
  if (data_len <= 0 || total_data_len <= 0) {
    return MqttRx::IgnoreEmpty;
  }
  if (current_offset != 0 || data_len != total_data_len ||
      total_data_len >= static_cast<int>(kMqttCommandCapacity) || has_nul) {
    return MqttRx::IgnoreMalformed;
  }
  return MqttRx::Accept;
}

void mqtt_sanitize(const char *src, char *dst, size_t dst_size) {
  if (dst == nullptr || dst_size == 0) {
    return;
  }
  size_t n = 0;
  if (src != nullptr) {
    for (; src[n] != '\0' && n + 1 < dst_size; n++) {
      const unsigned char c = static_cast<unsigned char>(src[n]);
      dst[n] = (c >= 0x20 && c <= 0x7e) ? static_cast<char>(c) : '?';
    }
  }
  dst[n] = '\0';
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all suites PASS, including `test_mqtt_proto` (13 cases); total 64 test cases succeeded (51 existing + 13).

- [ ] **Step 6: Commit**

```bash
git add EVSim/lib/mqtt_proto EVSim/test/test_mqtt_proto
git commit -m "Add the pure MQTT acceptance rules

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `net_mqtt` — the esp-mqtt wrapper

**Files:**
- Create: `EVSim/src/mqtt_config.h`
- Create: `EVSim/src/net_mqtt.h`
- Create: `EVSim/src/net_mqtt.cpp`

**Interfaces:**
- Consumes: `kMqttCommandCapacity`, `MqttRx`, `mqtt_rx_filter()` from Task 2; `PilotState`, `pilot_state_name()` from `lib/pilot_state`.
- Produces (used by Task 4):
  - `void net_mqtt_begin();`
  - `bool net_mqtt_take_command(char *out, size_t capacity);` — `capacity` must be ≥ `kMqttCommandCapacity`; returns `false` when nothing is queued.
  - `void net_mqtt_publish_state(PilotState state);`
  - `void net_mqtt_publish_reply(const char *text);` — `text` is copied, truncated to 63 characters.
  - `void net_mqtt_print_status();`

- [ ] **Step 1: Write `src/mqtt_config.h`**

```cpp
// The MQTT broker the board talks to, and the topics it uses.
//
// The broker is addressed by IP, never by hostname: this network has no
// internet route and DNS is unconfirmed (see wifi_config.h). Stage one runs
// Mosquitto on the development computer (192.168.21.137); when a separate
// server takes over, only kMqttBrokerUri changes.
//
// No credentials: stage one is an unauthenticated broker on the laboratory LAN.
// Revisit when the broker leaves the computer.
#pragma once

constexpr char kMqttBrokerUri[] = "mqtt://192.168.21.137:1883";
constexpr char kMqttClientId[] = "evsim";

// Commands in; the state, replies and liveness out. See the spec's topic table.
constexpr char kMqttTopicCmdState[] = "evsim/cmd/state";
constexpr char kMqttTopicState[] = "evsim/state";
constexpr char kMqttTopicReply[] = "evsim/reply";
constexpr char kMqttTopicStatus[] = "evsim/status";

// The broker publishes the last-will about 1.5 x this after the board vanishes.
constexpr int kMqttKeepaliveS = 10;

// esp-mqtt's own retry interval toward an unreachable broker.
constexpr int kMqttReconnectMs = 5000;
```

- [ ] **Step 2: Write `src/net_mqtt.h`**

```cpp
// MQTT command and status link, over the WiFi connection net_wifi maintains.
//
// The invariant, stronger than net_wifi's: loop() NEVER calls into the esp-mqtt
// client. Its publish/enqueue/start/stop calls can block for seconds (network
// timeout, or the client lock while its task connects to a dead broker), and no
// network condition may delay CP sampling. Everything that touches the client
// runs in esp-mqtt's own task or in one small pump task; loop() talks to them
// only through two FreeRTOS queues, always with a timeout of 0. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md.
#pragma once

#include <stddef.h>

#include "pilot_state.h"

// Creates the queues, the client and the pump task. Returns immediately; the
// link is not up yet. Call after net_wifi_begin(). If any allocation fails,
// prints one '#' line and leaves MQTT disabled -- the rest of the firmware is
// unaffected.
void net_mqtt_begin();

// Copies the oldest queued command into `out` (NUL-terminated) and returns
// true, or returns false at once when none is queued. `capacity` must be at
// least kMqttCommandCapacity. Call at most once per loop pass.
bool net_mqtt_take_command(char *out, size_t capacity);

// Records `state` as the state to announce on (re)connect and queues it for
// publishing on evsim/state. Never blocks; a full queue drops the item.
void net_mqtt_publish_state(PilotState state);

// Queues one line for evsim/reply. Never blocks; a full queue drops the line.
void net_mqtt_publish_reply(const char *text);

// Prints broker, connection flags and drop counters as '#' lines, for the 'W'
// command.
void net_mqtt_print_status();
```

- [ ] **Step 3: Write `src/net_mqtt.cpp`**

```cpp
#include "net_mqtt.h"

#include <Arduino.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <mqtt_client.h>
#include <string.h>

#include <atomic>

#include "mqtt_config.h"
#include "mqtt_proto.h"

namespace {

struct RxItem {
  char text[kMqttCommandCapacity];
};

enum class TxKind : uint8_t { State, Reply };

struct TxItem {
  TxKind kind;
  char text[64];
};

constexpr UBaseType_t kRxDepth = 4;
constexpr UBaseType_t kTxDepth = 8;
constexpr uint32_t kPumpPeriodMs = 100;

esp_mqtt_client_handle_t g_client = nullptr;
QueueHandle_t g_rx = nullptr;
QueueHandle_t g_tx = nullptr;

// Written by one task each, read by others; atomics rather than volatile so the
// counters do not tear.
std::atomic<bool> g_started{false};    // pump task
std::atomic<bool> g_connected{false};  // esp-mqtt task
std::atomic<uint8_t> g_last_state{0};  // a PilotState as int; A after reset
std::atomic<uint32_t> g_ignored_rx{0};
std::atomic<uint32_t> g_dropped_rx{0};
std::atomic<uint32_t> g_dropped_tx{0};

// Runs in the esp-mqtt task. Must stay short and must not touch Serial.
esp_err_t on_event(esp_mqtt_event_handle_t event) {
  switch (event->event_id) {
    case MQTT_EVENT_CONNECTED: {
      g_connected = true;
      esp_mqtt_client_subscribe(event->client, kMqttTopicCmdState, 1);
      esp_mqtt_client_publish(event->client, kMqttTopicStatus, "online", 0, 1, 1);
      const char *name =
          pilot_state_name(static_cast<PilotState>(g_last_state.load()));
      esp_mqtt_client_publish(event->client, kMqttTopicState, name, 0, 1, 1);
      break;
    }
    case MQTT_EVENT_DISCONNECTED:
      g_connected = false;
      break;
    case MQTT_EVENT_DATA: {
      const bool has_nul =
          event->data_len > 0 && event->data != nullptr &&
          memchr(event->data, '\0', event->data_len) != nullptr;
      const MqttRx verdict =
          mqtt_rx_filter(event->retain, event->data_len, event->total_data_len,
                         event->current_data_offset, has_nul);
      if (verdict != MqttRx::Accept) {
        g_ignored_rx++;
        break;
      }
      RxItem item;
      memcpy(item.text, event->data, event->data_len);
      item.text[event->data_len] = '\0';
      if (xQueueSend(g_rx, &item, 0) != pdTRUE) {
        g_dropped_rx++;
      }
      break;
    }
    default:
      break;
  }
  return ESP_OK;
}

// Owns the client's lifecycle and every publish. It may block inside esp-mqtt
// for as long as it likes; loop() never waits for it.
void pump_task(void *) {
  for (;;) {
    TxItem item;
    if (xQueueReceive(g_tx, &item, pdMS_TO_TICKS(kPumpPeriodMs)) == pdTRUE) {
      if (g_connected) {
        const bool is_state = item.kind == TxKind::State;
        esp_mqtt_client_enqueue(g_client,
                                is_state ? kMqttTopicState : kMqttTopicReply,
                                item.text, 0, 1, is_state ? 1 : 0, false);
      } else {
        g_dropped_tx++;
      }
    }

    const bool wifi_up = WiFi.status() == WL_CONNECTED;
    if (wifi_up && !g_started) {
      if (esp_mqtt_client_start(g_client) == ESP_OK) {
        g_started = true;
      }
    } else if (!wifi_up && g_started) {
      esp_mqtt_client_stop(g_client);
      g_started = false;
      g_connected = false;
    }
  }
}

void push_tx(TxKind kind, const char *text) {
  if (g_tx == nullptr || text == nullptr) {
    return;
  }
  TxItem item;
  item.kind = kind;
  snprintf(item.text, sizeof item.text, "%s", text);
  if (xQueueSend(g_tx, &item, 0) != pdTRUE) {
    g_dropped_tx++;
  }
}

}  // namespace

void net_mqtt_begin() {
  g_rx = xQueueCreate(kRxDepth, sizeof(RxItem));
  g_tx = xQueueCreate(kTxDepth, sizeof(TxItem));

  esp_mqtt_client_config_t config = {};
  config.uri = kMqttBrokerUri;
  config.client_id = kMqttClientId;
  config.event_handle = on_event;
  config.lwt_topic = kMqttTopicStatus;
  config.lwt_msg = "offline";
  config.lwt_msg_len = sizeof("offline") - 1;
  config.lwt_qos = 1;
  config.lwt_retain = 1;
  config.keepalive = kMqttKeepaliveS;
  config.reconnect_timeout_ms = kMqttReconnectMs;
  g_client = esp_mqtt_client_init(&config);

  if (g_rx == nullptr || g_tx == nullptr || g_client == nullptr ||
      xTaskCreatePinnedToCore(pump_task, "mqtt_pump", 4096, nullptr, 1, nullptr,
                              0) != pdPASS) {
    Serial.println("# net_mqtt: init failed -- MQTT disabled");
    g_client = nullptr;
    g_rx = nullptr;
    g_tx = nullptr;
  }
}

bool net_mqtt_take_command(char *out, size_t capacity) {
  if (g_rx == nullptr || out == nullptr || capacity < kMqttCommandCapacity) {
    return false;
  }
  RxItem item;
  if (xQueueReceive(g_rx, &item, 0) != pdTRUE) {
    return false;
  }
  memcpy(out, item.text, sizeof item.text);
  return true;
}

void net_mqtt_publish_state(PilotState state) {
  g_last_state = static_cast<uint8_t>(state);
  push_tx(TxKind::State, pilot_state_name(state));
}

void net_mqtt_publish_reply(const char *text) {
  push_tx(TxKind::Reply, text);
}

void net_mqtt_print_status() {
  if (g_client == nullptr) {
    Serial.println("# net_mqtt: disabled (init failed)");
    return;
  }
  Serial.print("# net_mqtt: broker ");
  Serial.print(kMqttBrokerUri);
  Serial.print(", client ");
  Serial.print(g_started ? "started" : "stopped");
  Serial.print(", ");
  Serial.println(g_connected ? "connected" : "not connected");
  Serial.print("# net_mqtt: ignored rx ");
  Serial.print(g_ignored_rx.load());
  Serial.print(", dropped rx ");
  Serial.print(g_dropped_rx.load());
  Serial.print(", dropped tx ");
  Serial.println(g_dropped_tx.load());
}
```

- [ ] **Step 4: Build both firmware environments**

Run: `~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage`
Expected: both `SUCCESS`. `net_mqtt.cpp` compiles but nothing calls it yet, so the link still succeeds (`src/*.cpp` is compiled regardless). If the compile stops on `mqtt_client.h` or an `esp_mqtt_*` symbol, the scratch-build result this plan rests on no longer holds — stop and report, do not add link flags blindly.

- [ ] **Step 5: Run the native tests**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: 64 test cases succeeded.

- [ ] **Step 6: Commit**

```bash
git add EVSim/src/mqtt_config.h EVSim/src/net_mqtt.h EVSim/src/net_mqtt.cpp
git commit -m "Add the esp-mqtt wrapper, kept off the loop() thread

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Wire MQTT into `main.cpp`

**Files:**
- Modify: `EVSim/src/main.cpp`

**Interfaces:**
- Consumes: everything Task 3 produces; `mqtt_sanitize()` and `kMqttCommandCapacity` from Task 2.
- Produces: the finished firmware.

- [ ] **Step 1: Update the header comment and includes**

In `src/main.cpp`, replace the file's opening comment lines 1–15 and the includes so they read:

```cpp
// EVSim -- reads the Control Pilot PWM the MENNEKES AMTRON signals on CP and
// reports the current and power it corresponds to, and drives the pilot state
// (A, B or C) the board presents to the charger.
//
// The state is commanded over serial: send A, B or C followed by a newline. It
// can also be commanded over MQTT, by publishing A, B or C on evsim/cmd/state;
// both channels go through the same handler. GPIO32 (state C) is only ever
// asserted while GPIO33 (state B) is, so A -> C is refused. The state is always
// A after reset. Send W over serial for WiFi and MQTT status.
//
// The board also joins the laboratory WLAN on a fixed address (wifi_config.h)
// and keeps an MQTT link to the broker (mqtt_config.h). Neither may block this
// loop: no network condition may delay CP sampling. Publishing the CP
// measurements over MQTT comes later.
//
// Output is CSV sharing column names with tools/charger_poll.py, so the two
// logs can be merged on time to produce the calibration curve. Replies to
// commands are '#'-prefixed lines, so the CSV stays parseable.

#include <Arduino.h>
#include <cmath>
#include <stdio.h>

#include "cp_convert.h"
#include "cp_reader.h"
#include "mqtt_proto.h"
#include "net_mqtt.h"
#include "net_wifi.h"
#include "pilot_driver.h"
#include "pilot_state.h"
```

- [ ] **Step 2: Replace `handle_command`**

Replace the whole `handle_command` function (currently `void handle_command(const char *line) { ... }`) with the following, and add the `Source` enum and `reply()` immediately above it:

```cpp
// Where a command came from. A command from MQTT is answered there as well as
// on serial, and cannot ask for the serial-only status report.
enum class Source { Serial, Mqtt };

// Every reply is a '#' line on serial, so the CSV stays parseable; a command
// that arrived over MQTT is answered on evsim/reply too.
void reply(Source source, const char *text) {
  Serial.print("# ");
  Serial.println(text);
  if (source == Source::Mqtt) {
    net_mqtt_publish_reply(text);
  }
}

void handle_command(const char *line, Source source) {
  if (source == Source::Serial && is_status_command(line)) {
    net_wifi_print_status();
    net_mqtt_print_status();
    return;
  }

  char text[64];
  PilotState requested = PilotState::A;
  if (!pilot_parse_command(line, &requested)) {
    // The bytes are untrusted (they may come off the network): show only
    // printable ones, so the reply cannot break the '#' line or the CSV.
    char safe[kLineMax];
    mqtt_sanitize(line, safe, sizeof safe);
    snprintf(text, sizeof text, "unknown command '%s'", safe);
    reply(source, text);
    return;
  }

  if (requested == g_state) {
    snprintf(text, sizeof text, "already in %s", pilot_state_name(g_state));
    reply(source, text);
    return;
  }

  const PilotResult result = pilot_request(g_state, requested);
  if (!result.accepted) {
    snprintf(text, sizeof text, "refused %s -> %s: state B must be asserted first",
             pilot_state_name(g_state), pilot_state_name(requested));
    reply(source, text);
    return;
  }

  pilot_driver_apply(g_state, result.state);
  snprintf(text, sizeof text, "state %s -> %s", pilot_state_name(g_state),
           pilot_state_name(result.state));
  reply(source, text);
  g_state = result.state;
  net_mqtt_publish_state(g_state);
}
```

- [ ] **Step 3: Pass `Source::Serial` from `poll_serial`, and add `poll_mqtt`**

In `poll_serial()`, change the call `handle_command(g_line);` to `handle_command(g_line, Source::Serial);`.

Add this function directly after `poll_serial()` (still inside the anonymous namespace):

```cpp
// Handles at most one queued MQTT command per loop pass, like poll_serial(), so
// a burst of commands cannot delay the CP sampling. Never waits.
void poll_mqtt() {
  char command[kMqttCommandCapacity];
  if (net_mqtt_take_command(command, sizeof command)) {
    handle_command(command, Source::Mqtt);
  }
}
```

- [ ] **Step 4: Start MQTT in `setup()` and poll it in `loop()`**

In `setup()`, directly after the `net_wifi_begin();` line, add:

```cpp
  // After net_wifi_begin(): the client only starts once WiFi reports connected.
  // Non-blocking, like everything network-related here.
  net_mqtt_begin();
```

Change the banner line `Serial.println("# send W for WiFi status");` to:

```cpp
  Serial.println("# send W for WiFi and MQTT status");
```

In `loop()`, change:

```cpp
  net_wifi_poll();
  poll_serial();
```

to:

```cpp
  net_wifi_poll();
  poll_serial();
  poll_mqtt();
```

- [ ] **Step 5: Build both firmware environments and run the native tests**

Run: `~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage && ~/.platformio/penv/bin/pio test -e native`
Expected: both builds `SUCCESS`; 64 test cases succeeded.

- [ ] **Step 6: Flash and smoke-test serial, with no broker running**

Flash the environment you normally use (`~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage -t upload`, then `pio device monitor`). With **no** Mosquitto running:
- Expected: the CSV header and rows flow as before; `# net_wifi: ... Connected, ip 192.168.21.138` appears.
- Send `W`: expect the existing `# net_wifi:` lines followed by `# net_mqtt: broker mqtt://192.168.21.137:1883, client started, not connected`.
- Send `B`, `C`, `A`, then `C` from A: expect `# state A -> B`, `# state B -> C`, `# state C -> A`, `# refused A -> C: state B must be asserted first` — byte-identical to before this change.
- Send `x`: expect `# unknown command 'x'`.
- Throughout, CSV rows continue at 200 ms with no gap, although the client is retrying toward a dead broker.

- [ ] **Step 7: Commit**

```bash
git add EVSim/src/main.cpp
git commit -m "Take pilot-state commands from MQTT through the same handler

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Broker config, bench acceptance, and the jitter check

**Files:**
- Create: `tools/mosquitto-lab.conf`

**Interfaces:**
- Consumes: the finished firmware from Task 4.

- [ ] **Step 1: Write the broker config**

Create `tools/mosquitto-lab.conf` (from the repository root):

```
# Mosquitto for the EVSim bench. Mosquitto 2.x listens on localhost only by
# default; the board needs the LAN. Unauthenticated: laboratory LAN, stage one.
# Run:  /opt/homebrew/sbin/mosquitto -c tools/mosquitto-lab.conf -v
listener 1883 0.0.0.0
allow_anonymous true
```

- [ ] **Step 2: Start the broker and a subscriber**

Terminal 1 (repo root): `/opt/homebrew/sbin/mosquitto -c tools/mosquitto-lab.conf -v`
Terminal 2: `mosquitto_sub -h 192.168.21.137 -t 'evsim/#' -v`
Terminal 3: the board's serial monitor.

Reset the board. Expected on terminal 2, within about 5 s: `evsim/status online` and `evsim/state A`. `W` on the monitor now says `client started, connected`.

- [ ] **Step 3: Happy path**

In a new terminal run each line and check both the subscriber and the monitor:

```bash
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m B    # evsim/reply "state A -> B", evsim/state B
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m C    # "state B -> C", state C
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m B    # "state C -> B"
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m B    # "already in B"
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m ' a '  # lenient: "state B -> A"
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m C    # "refused A -> C: state B must be asserted first"; state stays A
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m X    # "unknown command 'X'"
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m W    # "unknown command 'W'" (W is serial-only)
```

Expected: exactly the replies in the comments; the monitor shows the same texts as `# ...` lines; `evsim/state` (retained) always equals the state the monitor's CSV `commanded` column shows. A command typed on the serial monitor also updates `evsim/state`.

- [ ] **Step 4: Review Focus 3 and 2 — hostile payloads**

```bash
printf 'B\n' | mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -s    # accepted: "state A -> B" (trailing newline tolerated)
printf 'A\0x' | mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -s  # ignored: no reply, no state change
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -n                  # empty: ignored
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m 'AAAAAAAAAAAAAAAAAAAA'  # 20 bytes: ignored
printf 'Q\001\377' | mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -s    # reply "unknown command 'Q??'"
```

Expected: the results in the comments; the CSV never shows a stray line; `W` on the monitor afterwards reports `ignored rx 3` (the NUL, empty and oversize messages).

- [ ] **Step 5: Review Focus 1 — a retained command is not replayed**

With the board in state A:

```bash
mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m B -r   # live delivery: executes once, "state A -> B"
```

Press the board's reset button. Expected: after reconnecting, the state is **A** (`evsim/state A`, CSV `commanded` A) and `W` shows `ignored rx 1`. Then clear the retained message: `mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -n -r`.

- [ ] **Step 6: Review Focus 5 — a burst**

```bash
for i in 1 2 3 4 5 6 7 8 9 10; do mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m B; mosquitto_pub -h 192.168.21.137 -t evsim/cmd/state -m A; done
```

Expected: the CSV shows no gap larger than the usual 200 ms; the board ends in a consistent state that matches `evsim/state`; `W` shows any overflow as `dropped rx N` (0 is fine).

- [ ] **Step 7: Review Focus 4 — the broker goes away while a state is held**

Command `B` then `C`. Stop Mosquitto (Ctrl-C in terminal 1). Expected: the CSV continues without a gap and `commanded` stays `C` throughout; `W` reports `not connected`. Restart Mosquitto. Expected within about 5 s: `evsim/status online` and `evsim/state C` (retained). While the broker is down, also send `A` on the serial monitor, then restart the broker: `evsim/state` must read `A` — the actual state — not a stale `C`.

Also confirm the last-will: with the board connected, unplug its USB power; about 15 s later the subscriber shows `evsim/status offline`.

- [ ] **Step 8: Jitter — does MQTT disturb CP sampling?**

The baseline is the firmware with WiFi but **no MQTT**: the spec commit `175dcff` (docs only on top of the code before this work). Build it in a separate worktree so the working tree is untouched:

```bash
git worktree add --detach ../evsim-baseline 175dcff
cd ../evsim-baseline/EVSim && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage -t upload
```

(Use the environment you normally flash.) Put the charger in the same limit and the board in state C for every capture, start each capture a couple of seconds after boot (the tool drops the first window), then from the repository root:

```bash
~/.platformio/penv/bin/python tools/cp_jitter.py capture --out data/mqtt_baseline.csv --seconds 60
# reflash this branch's firmware, broker RUNNING and connected:
~/.platformio/penv/bin/python tools/cp_jitter.py capture --out data/mqtt_on.csv --seconds 60
# broker STOPPED, so the client retries continuously:
~/.platformio/penv/bin/python tools/cp_jitter.py capture --out data/mqtt_retrying.csv --seconds 60
python3 tools/cp_jitter.py compare data/mqtt_baseline.csv data/mqtt_on.csv
python3 tools/cp_jitter.py compare data/mqtt_baseline.csv data/mqtt_retrying.csv
```

Acceptance (the tool applies it): the `duty_pct` standard deviation grows by no more than **0.1 percentage points** and `freq_hz` stays within **1 Hz** of 1000 Hz, for **both** comparisons. `data/` and `*.csv` are gitignored.

If either fails, do not patch around it: report the numbers and stop. The escalation path, in order: check the pump task's 100 ms `WiFi.status()` polling and the esp-mqtt task priority (`config.task_prio`, default 5), the two things MQTT adds on the cores; anything larger (moving the CP capture off the ISR) needs its own design pass.

Then remove the baseline worktree: `git worktree remove ../evsim-baseline`.

- [ ] **Step 9: Commit**

```bash
git add tools/mosquitto-lab.conf
git commit -m "Add the Mosquitto config the bench needs to accept the board

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** topic table and QoS/retain flags → Task 3 (`on_event`, `pump_task`, LWT config) and Task 5 steps 2–3; rule 1 (retained ignored) → Task 2 test + Task 5 step 5; rule 2 (clean session) → esp-mqtt default (`disable_clean_session` left at 0) and Task 5 step 7; rule 3 (hold state) → nothing in Task 3/4 touches `g_state` on connection events, checked in Task 5 step 7; rule 4 (`online` + state on connect) → `MQTT_EVENT_CONNECTED`; rule 5 (every command replied) → `reply()` in Task 4, `W`-over-MQTT decision documented in Task 1; rule 6 (A after reset) → unchanged. Components, data flow, connection handling → Tasks 1, 3, 4. Testing section → Tasks 2 and 5. Broker setup → Task 5 step 1. The spec's "prefix" idea became four literal topic strings; Task 1 rewrites the Components text accordingly.

**Placeholder scan:** no TBD/TODO; every code step has its code. The one deliberate unknown — whether esp-mqtt's outbox can replay a stale `evsim/state` after reconnect — is a bench check (Task 5 step 7), not a placeholder.

**Type consistency:** `kMqttCommandCapacity` (Task 2) is used in Tasks 3 and 4 and equals `kLineMax` (16) in `main.cpp`, so `mqtt_sanitize(line, safe, sizeof safe)` and `net_mqtt_take_command(command, sizeof command)` both satisfy their size requirements. `mqtt_rx_filter` has five parameters in the header, implementation, tests and `on_event`. `net_mqtt_*` signatures match between `net_mqtt.h`, `net_mqtt.cpp` and `main.cpp`. `MqttRx` has four values everywhere.

**Known limits, stated rather than hidden:** the esp-mqtt task is not pinned to a core (the config struct has no such field); Task 5 step 8 measures whether that matters. Replies dropped while disconnected are not retried. No authentication (stage one).
