# WiFi Station with a Static IP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the EVSim ESP32 on the laboratory WLAN `hems` at the fixed address `192.168.21.138/24`, on a network with no DHCP server, without ever letting a network condition stall the 200 ms CP sampling loop.

**Architecture:** The connection policy — association timeout and bounded exponential backoff — lives as a pure function in `lib/net_state/`, so it is unit-tested on the Mac with simulated clocks rather than by waiting for real seconds. A thin `src/net_wifi.*` shim maps that policy onto the Arduino `WiFi` calls and does no waiting of any kind. `main.cpp` gains two calls and one serial command. Nothing else changes.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev`, Unity tests on the `native` env. `WiFi.h` ships with arduino-esp32, so `platformio.ini` needs no `lib_deps` and is not modified. `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. All commands below run from the `EVSim/` directory unless stated otherwise.

**Spec:** `docs/superpowers/specs/2026-09-25-wifi-static-ip-design.md`

## Global Constraints

- Network parameters, exact values: SSID `hems`, password `HEMS1CoSES`, IP `192.168.21.138`, mask `255.255.255.0`, gateway `192.168.21.130`, DNS `192.168.21.130` (assumed, unconfirmed).
- **`WiFi.config()` must be called before `WiFi.begin()`.** Reversed, the board associates and then waits out a DHCP lease that will never arrive on this network.
- **`WiFi.persistent(false)` before anything else.** It stops the SDK writing credentials to NVS; each such flash write disables the instruction cache on *both* cores, which is the one mechanism by which WiFi can genuinely disturb the `IRAM_ATTR` CP ISR.
- Timing constants, as named constants and never literals: `kNetConnectTimeoutMs = 10000`, `kNetBackoffMinMs = 1000`, `kNetBackoffMaxMs = 30000`. Backoff sequence must be 1 s, 2 s, 4 s, 8 s, 16 s, 30 s, 30 s, … and must reset to 1 s on a successful connection.
- `lib/net_state/` must **not** include `Arduino.h` or any Arduino type — it builds for `[env:native]`. Use `<stdint.h>`.
- All elapsed-time arithmetic is unsigned subtraction (`now_ms - since_ms`), which is correct across the `millis()` 49-day wraparound. Never compare timestamps with `<` or `>`.
- **The CSV schema does not change.** `tools/charger_poll.py` merges logs on shared column names and previously recorded calibration logs must stay comparable. All network output goes to `#`-prefixed lines.
- **Do not modify** `lib/cp_convert/`, `lib/pilot_state/`, `src/cp_reader.*`, or `src/pilot_driver.*`. The only edit to existing code is `src/main.cpp`.
- `pilot_parse_command()` is **not** taught the letter `W`. `lib/pilot_state` is the IEC 61851 state model and a network status query is not a pilot state; `W` is recognised in `main.cpp` before the line reaches that parser.
- Peers are addressed **by IP, never by hostname**: there is no internet route and DNS is unconfirmed. Nothing may depend on NTP or wall-clock time; `millis()` stays the only timebase.
- `EVSim/.vscode/c_cpp_properties.json` and `Data_analysis_PWM.xlsx` may carry unrelated local changes. Never use `git add -A` or `git add .`; add only the files named in each task.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `lib/net_state/net_state.h` / `.cpp` | Pure connection policy: phase + link status + `now_ms` → next phase and action |
| `test/test_net_state/test_net_state.cpp` | Host-side Unity tests for that policy |
| `src/wifi_config.h` | The six network values, and nothing else |
| `src/net_wifi.h` / `.cpp` | Arduino shim: `WiFi` calls, status printing. Never blocks |
| `src/main.cpp` | Two new calls plus the `W` command |

---

### Task 1: Pure connection policy

**Files:**
- Create: `EVSim/lib/net_state/net_state.h`
- Create: `EVSim/lib/net_state/net_state.cpp`
- Create: `EVSim/test/test_net_state/test_net_state.cpp`

**Interfaces:**
- Consumes: nothing.
- Produces, relied on by Task 2:
  - `enum class NetPhase { Idle, Connecting, Connected, Backoff };`
  - `enum class NetAction { None, BeginConnect, Disconnect };`
  - `struct NetState { NetPhase phase; uint32_t phase_started_ms; uint32_t backoff_ms; };`
  - `struct NetStep { NetState state; NetAction action; };`
  - `constexpr uint32_t kNetConnectTimeoutMs = 10000;`
  - `constexpr uint32_t kNetBackoffMinMs = 1000;`
  - `constexpr uint32_t kNetBackoffMaxMs = 30000;`
  - `NetState net_state_init(uint32_t now_ms);`
  - `NetStep net_step(NetState current, bool link_up, uint32_t now_ms);`
  - `const char *net_phase_name(NetPhase phase);`

**Why a value-in/value-out function:** it mirrors the existing `pilot_request()` / `PilotResult` pair in `lib/pilot_state`. No mutation, no globals, and the clock is a parameter — which is the whole reason a 30-second backoff can be tested in microseconds.

- [ ] **Step 1: Write the header**

Create `EVSim/lib/net_state/net_state.h`:

```cpp
// WiFi connection policy: when to start an association attempt, when to give
// up on one, and how long to wait before retrying.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the host
// as well as on the ESP32. The caller supplies the clock, which is what makes
// the timeouts testable without waiting real seconds. See
// docs/superpowers/specs/2026-09-25-wifi-static-ip-design.md.
#pragma once

#include <stdint.h>

// Idle: nothing started yet. Connecting: association attempt in flight.
// Connected: the link is up. Backoff: waiting before the next attempt.
enum class NetPhase { Idle, Connecting, Connected, Backoff };

// What the caller must do to reach the returned state. None is the common case.
enum class NetAction { None, BeginConnect, Disconnect };

// An association attempt that has not succeeded within this is a failure.
constexpr uint32_t kNetConnectTimeoutMs = 10000;

// The first retry delay, and the ceiling it doubles up to. The ceiling matters:
// unbounded doubling would eventually stop retrying for hours, while a flat
// short retry would hammer the radio and add avoidable CPU contention to the
// CP ISR.
constexpr uint32_t kNetBackoffMinMs = 1000;
constexpr uint32_t kNetBackoffMaxMs = 30000;

struct NetState {
  NetPhase phase;
  uint32_t phase_started_ms;  // when `phase` was entered
  uint32_t backoff_ms;        // the delay the next Backoff will observe
};

struct NetStep {
  NetState state;    // the resulting state
  NetAction action;  // what the caller must do to reach it
};

// Starting state: Idle, with the backoff wound back to its minimum.
NetState net_state_init(uint32_t now_ms);

// Applies the policy. Takes the current state by value and returns the next one
// together with the action that gets there; the caller owns the single live
// NetState and simply assigns the result back.
//
// `link_up` is the observed link status (on the ESP32, WiFi.status() ==
// WL_CONNECTED). A link that comes up on its own while in Backoff is accepted
// rather than torn down.
NetStep net_step(NetState current, bool link_up, uint32_t now_ms);

// Stable short name for logging: "Idle", "Connecting", "Connected", "Backoff".
const char *net_phase_name(NetPhase phase);
```

- [ ] **Step 2: Write the failing tests**

Create `EVSim/test/test_net_state/test_net_state.cpp`:

```cpp
// Host-side tests for the WiFi connection policy. Run with:
//   pio test -e native
#include <unity.h>

#include "net_state.h"

// Drives the machine to Connected at t=0 and returns the state, so tests about
// what happens after a successful connection do not each repeat the climb.
static NetState connected_state(void) {
  NetState s = net_state_init(0);
  s = net_step(s, false, 0).state;   // Idle -> Connecting
  return net_step(s, true, 5).state; // link came up
}

// --- getting connected -----------------------------------------------------

void test_init_starts_idle_with_the_minimum_backoff(void) {
  const NetState s = net_state_init(1234);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Idle, (int)s.phase);
  TEST_ASSERT_EQUAL_UINT32(1234, s.phase_started_ms);
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);
}

void test_idle_begins_connecting_immediately(void) {
  const NetStep r = net_step(net_state_init(0), false, 0);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)r.action);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)r.state.phase);
}

void test_link_up_while_connecting_reaches_connected_with_no_action(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  const NetStep r = net_step(s, true, 200);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

void test_staying_connected_does_nothing(void) {
  const NetStep r = net_step(connected_state(), true, 60000);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

// --- failing to connect ----------------------------------------------------

void test_connecting_holds_until_the_timeout_then_backs_off(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;

  // One millisecond short of the timeout: still trying, no action.
  const NetStep waiting = net_step(s, false, kNetConnectTimeoutMs - 1);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)waiting.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)waiting.action);

  // At the timeout: give up, and tell the caller to tear the attempt down so
  // the SDK stops retrying on its own before the next begin.
  const NetStep timed_out = net_step(s, false, kNetConnectTimeoutMs);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)timed_out.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::Disconnect, (int)timed_out.action);
}

void test_backoff_issues_no_retry_before_the_delay_elapses(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  s = net_step(s, false, kNetConnectTimeoutMs).state;  // -> Backoff, wait 1000

  const uint32_t entered = s.phase_started_ms;
  const NetStep early = net_step(s, false, entered + kNetBackoffMinMs - 1);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)early.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)early.action);

  const NetStep due = net_step(s, false, entered + kNetBackoffMinMs);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)due.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)due.action);
}

void test_backoff_doubles_and_caps_at_the_maximum(void) {
  // Walk failure after failure and record the delay each Backoff observes.
  const uint32_t expected[] = {1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000};
  const size_t count = sizeof(expected) / sizeof(expected[0]);

  NetState s = net_state_init(0);
  uint32_t now = 0;
  s = net_step(s, false, now).state;  // Idle -> Connecting

  for (size_t i = 0; i < count; i++) {
    now += kNetConnectTimeoutMs;
    s = net_step(s, false, now).state;  // Connecting -> Backoff
    TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)s.phase);

    // The delay in force for this Backoff is what the state carries now.
    TEST_ASSERT_EQUAL_UINT32(expected[i], s.backoff_ms);

    now += expected[i];
    s = net_step(s, false, now).state;  // Backoff -> Connecting
    TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)s.phase);
  }

  // Asserted at the cap, not merely below it.
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMaxMs, s.backoff_ms);
}

void test_a_successful_connection_resets_the_backoff(void) {
  // Fail twice so the backoff has grown past its minimum, then succeed.
  NetState s = net_state_init(0);
  uint32_t now = 0;
  s = net_step(s, false, now).state;
  for (int i = 0; i < 2; i++) {
    now += kNetConnectTimeoutMs;
    s = net_step(s, false, now).state;  // -> Backoff
    now += s.backoff_ms;
    s = net_step(s, false, now).state;  // -> Connecting
  }
  TEST_ASSERT_TRUE(s.backoff_ms > kNetBackoffMinMs);

  now += 100;
  s = net_step(s, true, now).state;  // connected at last
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)s.phase);
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);

  // Prove it by failing again: the wait must be the minimum, not the old value.
  now += 10;
  s = net_step(s, false, now).state;  // link lost -> Backoff
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);
}

// --- losing a working link -------------------------------------------------

void test_link_lost_while_connected_backs_off_and_reconnects(void) {
  NetState s = connected_state();

  const NetStep lost = net_step(s, false, 10000);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)lost.state.phase);

  // Because the backoff was reset on the earlier success, the board retries
  // about a second later rather than thirty.
  const NetStep retry = net_step(lost.state, false, 10000 + kNetBackoffMinMs);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)retry.action);
}

void test_a_link_that_returns_during_backoff_is_accepted(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  s = net_step(s, false, kNetConnectTimeoutMs).state;  // -> Backoff

  // The SDK reconnected by itself; do not tear it down just to redial.
  const NetStep r = net_step(s, true, s.phase_started_ms + 10);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

// --- the clock -------------------------------------------------------------

void test_timeouts_are_correct_across_the_millis_wraparound(void) {
  // Start the attempt 5 s before millis() wraps, so `now` is numerically far
  // smaller than `phase_started_ms` when the timeout is due.
  const uint32_t start = UINT32_MAX - 5000;
  NetState s = net_step(net_state_init(start), false, start).state;
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)s.phase);

  const uint32_t before = start + (kNetConnectTimeoutMs - 1);  // wraps
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting,
                        (int)net_step(s, false, before).state.phase);

  const uint32_t due = start + kNetConnectTimeoutMs;  // wraps
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff,
                        (int)net_step(s, false, due).state.phase);
}

// --- names -----------------------------------------------------------------

void test_phase_names(void) {
  TEST_ASSERT_EQUAL_STRING("Idle", net_phase_name(NetPhase::Idle));
  TEST_ASSERT_EQUAL_STRING("Connecting", net_phase_name(NetPhase::Connecting));
  TEST_ASSERT_EQUAL_STRING("Connected", net_phase_name(NetPhase::Connected));
  TEST_ASSERT_EQUAL_STRING("Backoff", net_phase_name(NetPhase::Backoff));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_init_starts_idle_with_the_minimum_backoff);
  RUN_TEST(test_idle_begins_connecting_immediately);
  RUN_TEST(test_link_up_while_connecting_reaches_connected_with_no_action);
  RUN_TEST(test_staying_connected_does_nothing);
  RUN_TEST(test_connecting_holds_until_the_timeout_then_backs_off);
  RUN_TEST(test_backoff_issues_no_retry_before_the_delay_elapses);
  RUN_TEST(test_backoff_doubles_and_caps_at_the_maximum);
  RUN_TEST(test_a_successful_connection_resets_the_backoff);
  RUN_TEST(test_link_lost_while_connected_backs_off_and_reconnects);
  RUN_TEST(test_a_link_that_returns_during_backoff_is_accepted);
  RUN_TEST(test_timeouts_are_correct_across_the_millis_wraparound);
  RUN_TEST(test_phase_names);
  return UNITY_END();
}
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd EVSim && ~/.platformio/penv/bin/pio test -e native -f test_net_state
```

Expected: a **compile/link failure**, not a test failure — `net_state.cpp` does not exist yet, so the undefined references are `net_state_init`, `net_step` and `net_phase_name`. If it compiles, something is wrong: check that `lib/net_state/net_state.cpp` really is absent.

- [ ] **Step 4: Write the implementation**

Create `EVSim/lib/net_state/net_state.cpp`:

```cpp
#include "net_state.h"

namespace {

// Unsigned subtraction, so this stays correct when millis() wraps past
// UINT32_MAX between `since_ms` and `now_ms`.
uint32_t elapsed(uint32_t now_ms, uint32_t since_ms) {
  return now_ms - since_ms;
}

// Doubles the delay without ever overflowing or overshooting the ceiling.
uint32_t doubled(uint32_t backoff_ms) {
  if (backoff_ms >= kNetBackoffMaxMs / 2) {
    return kNetBackoffMaxMs;
  }
  return backoff_ms * 2;
}

}  // namespace

NetState net_state_init(uint32_t now_ms) {
  NetState s{};
  s.phase = NetPhase::Idle;
  s.phase_started_ms = now_ms;
  s.backoff_ms = kNetBackoffMinMs;
  return s;
}

NetStep net_step(NetState current, bool link_up, uint32_t now_ms) {
  NetStep out{};
  out.state = current;
  out.action = NetAction::None;

  switch (current.phase) {
    case NetPhase::Idle:
      out.state.phase = NetPhase::Connecting;
      out.state.phase_started_ms = now_ms;
      out.action = NetAction::BeginConnect;
      break;

    case NetPhase::Connecting:
      if (link_up) {
        out.state.phase = NetPhase::Connected;
        out.state.phase_started_ms = now_ms;
        // A working link earns a fresh budget, so a brief AP reboot after days
        // of uptime costs about a second rather than the last grown delay.
        out.state.backoff_ms = kNetBackoffMinMs;
      } else if (elapsed(now_ms, current.phase_started_ms) >= kNetConnectTimeoutMs) {
        out.state.phase = NetPhase::Backoff;
        out.state.phase_started_ms = now_ms;
        // Tear the attempt down rather than leaving the SDK retrying underneath
        // the policy that has just decided to wait.
        out.action = NetAction::Disconnect;
      }
      break;

    case NetPhase::Connected:
      if (!link_up) {
        out.state.phase = NetPhase::Backoff;
        out.state.phase_started_ms = now_ms;
        // No Disconnect: the link is already gone, there is nothing to tear down.
      }
      break;

    case NetPhase::Backoff:
      if (link_up) {
        out.state.phase = NetPhase::Connected;
        out.state.phase_started_ms = now_ms;
        out.state.backoff_ms = kNetBackoffMinMs;
      } else if (elapsed(now_ms, current.phase_started_ms) >= current.backoff_ms) {
        out.state.phase = NetPhase::Connecting;
        out.state.phase_started_ms = now_ms;
        // The delay grows only when it is actually spent, so `backoff_ms` always
        // reads as the wait the current Backoff is observing.
        out.state.backoff_ms = doubled(current.backoff_ms);
        out.action = NetAction::BeginConnect;
      }
      break;
  }

  return out;
}

const char *net_phase_name(NetPhase phase) {
  switch (phase) {
    case NetPhase::Idle:       return "Idle";
    case NetPhase::Connecting: return "Connecting";
    case NetPhase::Connected:  return "Connected";
    case NetPhase::Backoff:    return "Backoff";
  }
  return "?";
}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd EVSim && ~/.platformio/penv/bin/pio test -e native -f test_net_state
```

Expected: PASS, 12 tests, 0 failures.

- [ ] **Step 6: Run the whole native suite, to prove nothing else broke**

```bash
cd EVSim && ~/.platformio/penv/bin/pio test -e native
```

Expected: `test_cp_convert`, `test_pilot_state` and `test_net_state` all pass.

- [ ] **Step 7: Commit**

```bash
git add EVSim/lib/net_state/net_state.h EVSim/lib/net_state/net_state.cpp \
        EVSim/test/test_net_state/test_net_state.cpp
git commit -m "$(cat <<'EOF'
Add the pure WiFi connection policy

Association timeout and bounded exponential backoff as a value-in/value-out
function, so a 30-second retry ceiling is tested in microseconds instead of
by waiting. Mirrors pilot_request()/PilotResult; no Arduino dependency, so it
builds on the host.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The Arduino shim and the network configuration

**Files:**
- Create: `EVSim/src/wifi_config.h`
- Create: `EVSim/src/net_wifi.h`
- Create: `EVSim/src/net_wifi.cpp`

**Interfaces:**
- Consumes, from Task 1: `NetPhase`, `NetAction`, `NetState`, `NetStep`, `kNetBackoffMinMs`, `net_state_init(uint32_t)`, `net_step(NetState, bool, uint32_t)`, `net_phase_name(NetPhase)`.
- Produces, relied on by Task 3:
  - `void net_wifi_begin();`
  - `void net_wifi_poll();`
  - `void net_wifi_print_status();`

There are no unit tests in this task: the shim is nothing but `WiFi` calls and printing, which cannot be exercised on the host. That is the point of having pushed every decision into Task 1. Its deliverable is a clean `esp32dev` build; behaviour is verified on hardware in Task 3.

- [ ] **Step 1: Write the configuration header**

Create `EVSim/src/wifi_config.h`:

```cpp
// The laboratory WLAN the board joins, and the address it takes there.
//
// The network runs NO DHCP server, so the address is fixed here rather than
// leased. All three devices in the thesis loop -- the AMTRON charger
// (192.168.21.117), this board, and the Fronius inverter (192.168.21.122) --
// share 192.168.21.0/24, so none of that traffic depends on the gateway.
//
// There is no internet route on this network: no NTP, hence no wall-clock time,
// and DNS is unconfirmed. Address every peer by IP, never by hostname.
//
// NOTE: these credentials are committed deliberately, which writes them into
// this repository's history permanently. Everything secret lives in this one
// file and nothing else reads it, so moving to a gitignored copy plus a
// committed wifi_config.example.h is a .gitignore line and a file copy, with no
// code change anywhere. Do that before making this repository public.
#pragma once

#include <IPAddress.h>

constexpr char kWifiSsid[] = "hems";
constexpr char kWifiPassword[] = "HEMS1CoSES";

const IPAddress kStaticIp(192, 168, 21, 138);
const IPAddress kSubnetMask(255, 255, 255, 0);
const IPAddress kGateway(192, 168, 21, 130);

// Assumed, not confirmed: the WLAN gateway is the likely DNS relay. Nothing in
// the firmware resolves a hostname, so a dead value here costs nothing.
const IPAddress kDns(192, 168, 21, 130);
```

- [ ] **Step 2: Write the shim header**

Create `EVSim/src/net_wifi.h`:

```cpp
// Joins the laboratory WLAN as a station on the fixed address in wifi_config.h.
//
// Nothing here ever blocks. The policy in lib/net_state decides when to dial
// and when to wait; this file only performs the WiFi calls that policy asks for
// and reports what happened on '#'-prefixed lines, so the CSV stays parseable.
//
// The invariant: no network condition may stop, delay or corrupt CP
// measurement. The board is a bench instrument first and a network node second.
#pragma once

// Configures the interface and starts the first association attempt. Returns
// immediately -- the link will not be up yet.
void net_wifi_begin();

// Advances the connection policy. Call every loop pass; it never waits.
void net_wifi_poll();

// Prints phase, addresses and signal quality as '#' lines, for the 'W' command.
void net_wifi_print_status();
```

- [ ] **Step 3: Write the shim**

Create `EVSim/src/net_wifi.cpp`:

```cpp
#include "net_wifi.h"

#include <Arduino.h>
#include <WiFi.h>

#include "net_state.h"
#include "wifi_config.h"

namespace {

NetState g_state;
bool g_begun = false;

}  // namespace

void net_wifi_begin() {
  // Before anything else: stop the SDK persisting credentials to NVS. Every
  // such write is a flash operation, and flash writes disable the instruction
  // cache on BOTH cores -- the one mechanism by which WiFi can genuinely
  // disturb the IRAM_ATTR CP ISR. The credentials are compiled in, so
  // persisting them buys nothing.
  WiFi.persistent(false);

  WiFi.mode(WIFI_STA);  // station only; never bring up the soft AP

  // Modem sleep adds tens of milliseconds of latency to inbound packets, which
  // would later show up as laggy pilot-state commands over MQTT.
  WiFi.setSleep(false);

  // MUST precede WiFi.begin(). Called after, the board associates and then
  // waits out a DHCP lease that will never arrive on this network, which
  // presents as "connects, but has no IP".
  if (!WiFi.config(kStaticIp, kGateway, kSubnetMask, kDns)) {
    Serial.println("# net_wifi: WiFi.config() failed -- static IP NOT applied");
  }

  g_state = net_state_init(millis());
  g_begun = true;

  // Idle -> Connecting on this same pass, so the attempt starts now rather than
  // one loop later. Still non-blocking: it only calls WiFi.begin().
  net_wifi_poll();
}

void net_wifi_poll() {
  if (!g_begun) {
    return;
  }

  const NetPhase was = g_state.phase;
  const NetStep step = net_step(g_state, WiFi.status() == WL_CONNECTED, millis());
  g_state = step.state;

  switch (step.action) {
    case NetAction::BeginConnect:
      WiFi.begin(kWifiSsid, kWifiPassword);
      break;
    case NetAction::Disconnect:
      WiFi.disconnect();
      break;
    case NetAction::None:
      break;
  }

  if (g_state.phase == was) {
    return;
  }

  Serial.print("# net_wifi: ");
  Serial.print(net_phase_name(was));
  Serial.print(" -> ");
  Serial.print(net_phase_name(g_state.phase));
  if (g_state.phase == NetPhase::Connected) {
    Serial.print(", ip ");
    Serial.print(WiFi.localIP());
  } else if (g_state.phase == NetPhase::Backoff) {
    Serial.print(", retry in ");
    Serial.print(g_state.backoff_ms);
    Serial.print(" ms");
  }
  Serial.println();
}

void net_wifi_print_status() {
  // WiFi.status() is printed raw as well as the phase: it is what tells a wrong
  // password (WL_CONNECT_FAILED) apart from an absent AP (WL_NO_SSID_AVAIL),
  // which the policy deliberately treats identically.
  Serial.print("# net_wifi: phase ");
  Serial.print(net_phase_name(g_state.phase));
  Serial.print(", WiFi.status() ");
  Serial.print(static_cast<int>(WiFi.status()));
  Serial.print(", ssid ");
  Serial.println(kWifiSsid);

  Serial.print("# net_wifi: ip ");
  Serial.print(WiFi.localIP());
  Serial.print(", mask ");
  Serial.print(WiFi.subnetMask());
  Serial.print(", gw ");
  Serial.println(WiFi.gatewayIP());

  Serial.print("# net_wifi: rssi ");
  Serial.print(WiFi.RSSI());
  Serial.print(" dBm, channel ");
  Serial.println(WiFi.channel());
}
```

- [ ] **Step 4: Verify it compiles for the ESP32**

```bash
cd EVSim && ~/.platformio/penv/bin/pio run -e esp32dev
```

Expected: SUCCESS. `net_wifi.cpp` is compiled even though nothing calls it yet — PlatformIO builds every file in `src/`. If the linker complains about `net_state`, check that `lib/net_state/` sits beside `lib/cp_convert/` so the library dependency finder picks it up.

- [ ] **Step 5: Verify the host tests still build and pass**

```bash
cd EVSim && ~/.platformio/penv/bin/pio test -e native
```

Expected: all three suites pass. This catches the mistake of having let an Arduino type leak into `lib/net_state/`, which would break the `native` build.

- [ ] **Step 6: Commit**

```bash
git add EVSim/src/wifi_config.h EVSim/src/net_wifi.h EVSim/src/net_wifi.cpp
git commit -m "$(cat <<'EOF'
Add the WiFi station shim and the lab network configuration

Configures the static address before WiFi.begin(), because the lab network
has no DHCP server and the reverse order waits out a lease that never comes.
WiFi.persistent(false) keeps the SDK from writing credentials to flash, whose
cache-disabling stalls are the real threat to the CP ISR.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire it into the firmware and verify on hardware

**Files:**
- Modify: `EVSim/src/main.cpp` — the file comment, the includes, `handle_command()`, `setup()` and `loop()`

**Interfaces:**
- Consumes, from Task 2: `net_wifi_begin()`, `net_wifi_poll()`, `net_wifi_print_status()`.
- Produces: nothing for later tasks. This is the last task.

- [ ] **Step 1: Add the include**

In `EVSim/src/main.cpp`, add `net_wifi.h` to the existing include block, keeping it alphabetical among the project headers:

```cpp
#include "cp_convert.h"
#include "cp_reader.h"
#include "net_wifi.h"
#include "pilot_driver.h"
#include "pilot_state.h"
```

- [ ] **Step 2: Update the file comment**

Replace this paragraph at the top of `main.cpp`:

```cpp
// The state is commanded over serial: send A, B or C followed by a newline.
// GPIO32 (state C) is only ever asserted while GPIO33 (state B) is, so A -> C
// is refused. The state is always A after reset. Publishing over MQTT, and
// taking commands from it, come later.
```

with:

```cpp
// The state is commanded over serial: send A, B or C followed by a newline.
// GPIO32 (state C) is only ever asserted while GPIO33 (state B) is, so A -> C
// is refused. The state is always A after reset. Send W for WiFi status.
//
// The board also joins the laboratory WLAN on a fixed address (wifi_config.h).
// That never blocks this loop: no network condition may delay CP sampling.
// Publishing over MQTT, and taking commands from it, come later.
```

- [ ] **Step 3: Add the status-command recogniser**

In the anonymous `namespace` in `main.cpp`, directly above `handle_command()`, add:

```cpp
// True for a line holding exactly one 'W' or 'w', ignoring surrounding
// whitespace -- the same leniency pilot_parse_command() gives A/B/C. Kept here
// rather than in lib/pilot_state, because that library is the IEC 61851 state
// model and a network status query is not a pilot state.
bool is_status_command(const char *line) {
  if (line == nullptr) {
    return false;
  }
  const char *p = line;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  if (*p != 'W' && *p != 'w') {
    return false;
  }
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  return *p == '\0';
}
```

- [ ] **Step 4: Dispatch `W` before the pilot parser**

In `handle_command()`, insert these lines as the very first statements of the function, above `PilotState requested = PilotState::A;`:

```cpp
  if (is_status_command(line)) {
    net_wifi_print_status();
    return;
  }
```

- [ ] **Step 5: Start the radio in `setup()`**

In `setup()`, find:

```cpp
  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim -- CP PWM reader on GPIO35, pilot state control on GPIO32/33");
  Serial.println("# send A, B or C to set the pilot state; A -> C is refused");
```

and replace it with:

```cpp
  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim -- CP PWM reader on GPIO35, pilot state control on GPIO32/33");
  Serial.println("# send A, B or C to set the pilot state; A -> C is refused");
  Serial.println("# send W for WiFi status");

  // After cp_reader_begin(), so CP measurement is live before the radio starts
  // -- the same reasoning that puts pilot_driver_begin() first. Non-blocking:
  // the link will still be down when this returns.
  net_wifi_begin();
```

The `Serial.println("millis,freq_hz,...")` header line and everything after it stay exactly as they are. **The CSV columns do not change**, so `tools/charger_poll.py` merges keep working against logs recorded before this change.

- [ ] **Step 6: Poll the policy in `loop()`**

In `loop()`, change:

```cpp
void loop() {
  poll_serial();
```

to:

```cpp
void loop() {
  net_wifi_poll();
  poll_serial();
```

Leave the `kSampleIntervalMs` deadline arithmetic below it untouched. Because `net_wifi_poll()` never waits, the 200 ms cadence is unaffected — the same property that makes `poll_serial()` safe today, where it dispatches at most one command per pass.

- [ ] **Step 7: Build and check the host tests**

```bash
cd EVSim && ~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio test -e native
```

Expected: build SUCCESS, all three native suites pass.

- [ ] **Step 8: Flash and confirm the link**

```bash
cd EVSim && ~/.platformio/penv/bin/pio run -e esp32dev -t upload && ~/.platformio/penv/bin/pio device monitor
```

Expected within a few seconds of boot:

```
# net_wifi: Idle -> Connecting
# net_wifi: Connecting -> Connected, ip 192.168.21.138
```

CSV rows must be flowing the whole time, including while `Connecting`. If they pause, stop here: something in the shim is blocking, which violates the design's central invariant.

- [ ] **Step 9: Confirm the addressing**

In the monitor, send `W`. Expected: phase `Connected`, ip `192.168.21.138`, mask `255.255.255.0`, gw `192.168.21.130`, and a plausible RSSI (roughly −30 to −80 dBm).

Then, from the Mac:

```bash
ping -c 4 192.168.21.138
```

Expected: 4 replies, 0% packet loss.

- [ ] **Step 10: Confirm the board and the charger coexist**

There is no `--once` flag; bound the run with `--duration`. The tool needs an Authorization
token, which expires — if this 401s, grab a fresh one from the charger's web UI
(`memory/evsim-amtron-rest-api.md` has the recipe):

```bash
cd .. && export EVSIM_TOKEN=<token from the charger web UI>
python3 tools/charger_poll.py --duration 3
```

Expected: a few sample lines naming `192.168.21.117`, not a connection error. This proves the
board and the charger sit on the subnet together. A 401 is *not* a failure of this task — it
means the token expired, and the connection itself worked.

- [ ] **Step 11: Confirm the pilot states are untouched**

In the monitor: send `B`, expect `# state A -> B`. Send `C`, expect `# state B -> C`. Send `A`, expect `# state C -> A`. Then send `C` directly from A and expect the refusal: `# refused A -> C: state B must be asserted first`.

- [ ] **Step 12: The jitter measurement — the point of the exercise**

Set a fixed current limit on the charger, present state C, and capture two 60-second CSV logs against it: one with the AP powered off (or out of range), one connected. Then compare:

```bash
# for each log, over rows whose state column is PwmValid
python3 - <<'PY'
import csv, statistics, sys
for path in ("wifi_off.csv", "wifi_on.csv"):
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r["state"] == "PwmValid"]
    for col in ("freq_hz", "duty_pct"):
        vals = [float(r[col]) for r in rows]
        print(f"{path} {col}: n={len(vals)} mean={statistics.mean(vals):.3f} "
              f"sd={statistics.pstdev(vals):.3f}")
PY
```

**Acceptance:** the `duty_pct` standard deviation grows by no more than **0.1 percentage points** — the resolution the reader claims — and `freq_hz` stays within **1 Hz** of 1000 Hz.

If it fails, do not patch around it: report the numbers and stop. The escalation path, in order, is a dedicated core-0 FreeRTOS task for the network layer, then the MCPWM capture unit. Both are larger changes that need their own design pass.

- [ ] **Step 13: Confirm reconnection**

With a log running, power-cycle the AP. Expected: a `Connected -> Backoff, retry in 1000 ms` line, then `Backoff -> Connecting`, then `Connecting -> Connected, ip 192.168.21.138` once the AP is back. **CSV rows must continue without a gap throughout.**

- [ ] **Step 14: Commit**

```bash
git add EVSim/src/main.cpp
git commit -m "$(cat <<'EOF'
Join the lab WLAN from the firmware, and add the W status command

net_wifi_poll() sits beside poll_serial() and never waits, so the 200 ms CP
cadence is unchanged. W is recognised in main.cpp rather than taught to
pilot_parse_command(), which models IEC 61851 states and not network queries.
The CSV columns are untouched so existing calibration logs still merge.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 15: Record the jitter numbers in memory**

Append the measured means and standard deviations from Step 12 to `memory/evsim-network.md` under a new `## Measured WiFi effect on CP sampling` heading, with the date. **Do this whether the result is good or bad** — a null result is exactly what `memory/evsim-firmware-layout.md` asks for when it says the MCPWM question should be revisited "only if WiFi introduces jitter", and an unrecorded measurement will simply be taken again.

```bash
git add memory/evsim-network.md
git commit -m "$(cat <<'EOF'
Record the measured WiFi effect on CP sampling

Answers the open question in the firmware layout note about whether WiFi
introduces enough jitter to justify the MCPWM capture unit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Not in this plan

Deliberately, per the spec:

- **MQTT.** The next design. It inherits two constraints: address the broker **by IP** (DNS is not trustworthy), and **hold the last valid setpoint** across the charger's expected no-load current oscillation (`memory/evsim-open-questions.md`).
- **OTA updates.** They compete with the ISR for CPU and would muddy the Step 12 measurement that this plan exists to take.
- **Duplicate-IP detection.** Out of scope; `192.168.21.138` is assumed reserved. The symptom to recognise later is "connects fine, `W` shows the right IP, nothing answers".
- **Memory corrections.** Already done, in commits `f53d3be` and `3bbc42b`.
