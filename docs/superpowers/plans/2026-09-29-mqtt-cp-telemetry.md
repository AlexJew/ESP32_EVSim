# MQTT CP Telemetry and Held Setpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish every 200 ms CP sample as one JSON message on `evsim/cp`, carrying the raw measurement plus a held power setpoint (`live` / `held` / `zero`), without ever letting the network delay CP sampling or displace the state-command traffic.

**Architecture:** Two pure libraries, unit-tested on the Mac: `lib/cp_setpoint` (the hold rules and the 30 s window) and `lib/cp_json` (formatting one sample, `null` for missing numbers). `net_mqtt` gains a one-slot overwrite mailbox that `loop()` writes without blocking; the existing pump task drains the control queue first, then the mailbox, and publishes at QoS 0. `main.cpp` adds a few lines to the existing sample tick and resets the hold on every pilot-state change.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev`, Unity tests on the `native` env, `esp-mqtt` (already in use), FreeRTOS queues. `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. `pio` commands run from the `EVSim/` directory; `git` commands run from the repository root, with repo-relative paths (`EVSim/lib/...`).

**Spec:** `docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md` (builds on `2026-09-29-mqtt-state-commands-design.md`, already merged).

## Global Constraints

- Topic `evsim/cp`. QoS 0, **not** retained, one message per 200 ms sample (5 Hz). Nothing is queued while the client is disconnected; the newest sample replaces an unsent older one.
- Payload, exact field names and order: `{"t":123456,"freq":1000.0,"duty":24.30,"adc":0,"cp":"PWM_VALID","state":"C","amps":14.58,"watts":10060,"setpoint_w":10060,"status":"live","age_ms":0}`. `freq` has 1 decimal, `duty` and `amps` 2, `watts` and `setpoint_w` none. `amps`, `watts` are `null` when missing; `age_ms` is `null` when unknown. No bare `nan`/`inf` token may ever appear.
- A sample is **valid** when `cp` is `PWM_VALID` **and** `watts` is a number. `cp` uses the CSV names (`NO_SIGNAL`, `DC_12V`, `PWM_VALID`, `OUT_OF_RANGE`); `state` is the pilot state `A`, `B` or `C`.
- Status rules: **`live`** — state C and valid (setpoint = this sample's watts, age 0). **`held`** — state C, not valid, a valid one seen since entering C, at most `kSetpointHoldMs` = 30000 ms old (setpoint = last valid value). **`zero`** — everything else (setpoint 0). Leaving state C forgets the held value. The hold window is the named constant `kSetpointHoldMs`.
- Elapsed time is unsigned subtraction (`now_ms - since_ms`), correct across the 49-day `millis()` wrap. Never compare timestamps with `<` or `>`.
- **`loop()` never calls any `esp_mqtt_client_*` function, never waits, and only uses queue calls with timeout 0** (`xQueueOverwrite` writes never block). Telemetry must not use the control queue `g_tx`, which carries state changes and replies.
- The board does **no smoothing or averaging**; every `live` message is one unfiltered reading.
- `lib/cp_setpoint/` and `lib/cp_json/` must **not** include `Arduino.h` or any Arduino/FreeRTOS type — they build for `[env:native]`.
- **The CSV schema does not change**; MQTT diagnostics stay `#`-prefixed lines printed only from `loop()`'s thread.
- Do **not** modify `lib/cp_convert/`, `lib/pilot_state/`, `lib/net_state/`, `lib/cp_adc_level/`, `lib/mqtt_proto/`, `src/cp_reader_*`, `src/pilot_driver.*`, or `src/net_wifi.*`. Edited files: `src/net_mqtt.{h,cpp}`, `src/mqtt_config.h`, `src/main.cpp`.
- Never `git add -A` or `git add .`; `data/` may hold untracked captures. Add only the files named in each task.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Most likely first; each line has a test in the task that owns the code.

1. **A held value must not leak** across leaving and re-entering state C, including a quick C → B → C between two samples. Task 1 (leave-and-re-enter test) and Task 4 (the reset in `handle_command`).
2. **A sample with no current or power** (duty out of range, `cp` not `PWM_VALID`) must publish `null`, never a bare `nan`/`inf`, and a number too long for its field must become `null`, not a silently truncated number. Task 2.
3. **The hold across the `millis()` wrap** — a hold that spans the wrap stays `held` with the right age, and an already-expired hold is never revived by a wrap. Task 1.
4. **Telemetry must not delay sampling or displace control traffic** — `loop()` only overwrites a one-slot queue, and a burst of state commands during the 5 Hz stream is still published. Task 3 (code) and Task 5 (bench).
5. **Text that would make invalid JSON** (a quote, backslash or control character in `cp`/`state`/`status`) is rejected, not published. Task 2.

---

## File Structure

| File | Responsibility |
|---|---|
| `EVSim/lib/cp_setpoint/cp_setpoint.h` / `.cpp` | Create. Pure: the live/held/zero rules and the 30 s window. |
| `EVSim/test/test_cp_setpoint/test_cp_setpoint.cpp` | Create. Host tests for the above. |
| `EVSim/lib/cp_json/cp_json.h` / `.cpp` | Create. Pure: format one sample as the JSON message. |
| `EVSim/test/test_cp_json/test_cp_json.cpp` | Create. Host tests for the above. |
| `EVSim/src/mqtt_config.h` | Modify. Add the `evsim/cp` topic. |
| `EVSim/src/net_mqtt.h` / `.cpp` | Modify. The one-slot mailbox, `net_mqtt_publish_cp`, the pump drains it, the `W` counter. |
| `EVSim/src/main.cpp` | Modify. Step the setpoint, format and publish in the sample tick; reset on state change. |

---

### Task 1: `lib/cp_setpoint` — the hold rules

**Files:**
- Create: `EVSim/lib/cp_setpoint/cp_setpoint.h`
- Create: `EVSim/lib/cp_setpoint/cp_setpoint.cpp`
- Test: `EVSim/test/test_cp_setpoint/test_cp_setpoint.cpp`

**Interfaces:**
- Produces (used by Task 4):
  - `constexpr uint32_t kSetpointHoldMs = 30000;`
  - `enum class SetpointStatus { Live, Held, Zero };`
  - `struct SetpointState { bool have_valid; bool expired; float last_watts; uint32_t last_valid_ms; };`
  - `struct SetpointStep { SetpointState state; SetpointStatus status; int32_t setpoint_w; bool age_known; uint32_t age_ms; };`
  - `SetpointState setpoint_init();`
  - `SetpointStep setpoint_step(SetpointState prev, bool in_state_c, bool sample_valid, float watts, uint32_t now_ms);`
  - `const char *setpoint_status_name(SetpointStatus status);` — `"live"`, `"held"`, `"zero"`.

- [ ] **Step 1: Write the failing test**

Create `test/test_cp_setpoint/test_cp_setpoint.cpp`:

```cpp
// Host-side tests for the held-setpoint rules. Run with:
//   pio test -e native
#include <math.h>
#include <string.h>
#include <unity.h>

#include "cp_setpoint.h"

static int st(SetpointStatus s) { return static_cast<int>(s); }

void test_a_valid_sample_in_state_c_is_live(void) {
  const SetpointStep r =
      setpoint_step(setpoint_init(), true, true, 10060.4f, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Live), st(r.status));
  TEST_ASSERT_EQUAL_INT(10060, r.setpoint_w);
  TEST_ASSERT_TRUE(r.age_known);
  TEST_ASSERT_EQUAL_UINT32(0, r.age_ms);
}

void test_the_setpoint_is_rounded_half_up_to_whole_watts(void) {
  const SetpointStep r =
      setpoint_step(setpoint_init(), true, true, 10060.5f, 1000);
  TEST_ASSERT_EQUAL_INT(10061, r.setpoint_w);
}

void test_a_gap_after_a_valid_sample_is_held_with_its_age(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 10000.0f, 1000);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 8400);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(b.status));
  TEST_ASSERT_EQUAL_INT(10000, b.setpoint_w);
  TEST_ASSERT_TRUE(b.age_known);
  TEST_ASSERT_EQUAL_UINT32(7400, b.age_ms);
}

void test_the_hold_lasts_exactly_the_window(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 10000.0f, 1000);
  const SetpointStep at_limit =
      setpoint_step(a.state, true, false, NAN, 1000 + kSetpointHoldMs);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(at_limit.status));
  TEST_ASSERT_EQUAL_INT(10000, at_limit.setpoint_w);

  const SetpointStep past =
      setpoint_step(a.state, true, false, NAN, 1000 + kSetpointHoldMs + 1);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(past.status));
  TEST_ASSERT_EQUAL_INT(0, past.setpoint_w);
  TEST_ASSERT_TRUE(past.age_known);
  TEST_ASSERT_EQUAL_UINT32(kSetpointHoldMs + 1, past.age_ms);
}

void test_state_c_with_no_valid_sample_yet_is_zero_with_unknown_age(void) {
  const SetpointStep r = setpoint_step(setpoint_init(), true, false, NAN, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(r.status));
  TEST_ASSERT_EQUAL_INT(0, r.setpoint_w);
  TEST_ASSERT_FALSE(r.age_known);
}

void test_outside_state_c_is_zero_even_for_a_valid_sample(void) {
  const SetpointStep r = setpoint_step(setpoint_init(), false, true, 8000.0f, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(r.status));
  TEST_ASSERT_EQUAL_INT(0, r.setpoint_w);
  TEST_ASSERT_FALSE(r.age_known);
}

void test_leaving_state_c_forgets_the_held_value(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 8000.0f, 1000);
  // The board is in state B for one sample, then back in C with a gap.
  const SetpointStep b = setpoint_step(a.state, false, false, NAN, 1200);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(b.status));
  TEST_ASSERT_FALSE(b.state.have_valid);
  const SetpointStep c = setpoint_step(b.state, true, false, NAN, 1400);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(c.status));
  TEST_ASSERT_EQUAL_INT(0, c.setpoint_w);
  TEST_ASSERT_FALSE(c.age_known);
}

void test_a_fresh_valid_sample_refreshes_the_hold(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 9000.0f, 1000);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 5000);
  TEST_ASSERT_EQUAL_UINT32(4000, b.age_ms);
  const SetpointStep c = setpoint_step(b.state, true, true, 9500.0f, 6000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Live), st(c.status));
  TEST_ASSERT_EQUAL_INT(9500, c.setpoint_w);
  const SetpointStep d = setpoint_step(c.state, true, false, NAN, 7000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(d.status));
  TEST_ASSERT_EQUAL_INT(9500, d.setpoint_w);
  TEST_ASSERT_EQUAL_UINT32(1000, d.age_ms);
}

void test_a_hold_that_spans_the_clock_wrap_keeps_the_right_age(void) {
  const uint32_t before_wrap = 0xFFFFFF00u;  // 256 ms before the counter wraps
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 7000.0f, before_wrap);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 500u);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(b.status));
  TEST_ASSERT_EQUAL_INT(7000, b.setpoint_w);
  TEST_ASSERT_EQUAL_UINT32(756, b.age_ms);  // 256 + 500
}

void test_an_expired_hold_is_not_revived_by_the_clock_wrap(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 5000.0f, 100);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 30101);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(b.status));
  // The counter later wraps so that (now - last_valid) is again under 30 s.
  const SetpointStep c = setpoint_step(b.state, true, false, NAN, 5100);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(c.status));
  TEST_ASSERT_EQUAL_INT(0, c.setpoint_w);
}

void test_status_names(void) {
  TEST_ASSERT_EQUAL_STRING("live", setpoint_status_name(SetpointStatus::Live));
  TEST_ASSERT_EQUAL_STRING("held", setpoint_status_name(SetpointStatus::Held));
  TEST_ASSERT_EQUAL_STRING("zero", setpoint_status_name(SetpointStatus::Zero));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_valid_sample_in_state_c_is_live);
  RUN_TEST(test_the_setpoint_is_rounded_half_up_to_whole_watts);
  RUN_TEST(test_a_gap_after_a_valid_sample_is_held_with_its_age);
  RUN_TEST(test_the_hold_lasts_exactly_the_window);
  RUN_TEST(test_state_c_with_no_valid_sample_yet_is_zero_with_unknown_age);
  RUN_TEST(test_outside_state_c_is_zero_even_for_a_valid_sample);
  RUN_TEST(test_leaving_state_c_forgets_the_held_value);
  RUN_TEST(test_a_fresh_valid_sample_refreshes_the_hold);
  RUN_TEST(test_a_hold_that_spans_the_clock_wrap_keeps_the_right_age);
  RUN_TEST(test_an_expired_hold_is_not_revived_by_the_clock_wrap);
  RUN_TEST(test_status_names);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `~/.platformio/penv/bin/pio test -e native -f test_cp_setpoint`
Expected: FAIL to build — `cp_setpoint.h: No such file or directory`.

- [ ] **Step 3: Write the header**

Create `lib/cp_setpoint/cp_setpoint.h`:

```cpp
// The held power setpoint: what the board tells its consumer to follow, given
// the stream of CP samples and the pilot state it presents.
//
// The charger's current offer vanishes for a few seconds every 6-12 s when
// nothing draws current, so publishing each sample's power would send 0 W in
// every gap. Instead the last valid value is held through a gap, up to a
// window; it drops to zero the moment the pilot leaves state C, and after the
// window if the charger never comes back. No averaging happens here: every
// Live value is one unfiltered reading, and smoothing is the consumer's job.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the
// host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md.
#pragma once

#include <stdint.h>

// How long the last valid setpoint is held once samples stop being valid.
constexpr uint32_t kSetpointHoldMs = 30000;

enum class SetpointStatus {
  Live,  // state C and this sample is valid
  Held,  // state C, no valid sample now, the last valid one is recent
  Zero,  // nothing to follow: not in state C, nothing valid yet, or expired
};

struct SetpointState {
  bool have_valid;        // a valid sample was seen since entering state C
  bool expired;           // the hold ran out; stays set until state C is left
  float last_watts;       // the last valid power
  uint32_t last_valid_ms; // when it was seen
};

struct SetpointStep {
  SetpointState state;   // carry this into the next call
  SetpointStatus status;
  int32_t setpoint_w;    // whole watts; 0 when Zero
  bool age_known;        // false until a valid sample was seen since entering C
  uint32_t age_ms;       // time since the last valid sample, when age_known
};

// Starting state, and the state after leaving state C: nothing remembered.
SetpointState setpoint_init();

// Advances the rules by one sample. `in_state_c` is whether the board presents
// pilot state C; `sample_valid` is whether this sample is a usable offer, and
// `watts` its power (ignored when not valid). Elapsed time is unsigned
// subtraction, correct across the 49-day millis() wrap; an expired hold stays
// expired so a later wrap cannot revive it.
SetpointStep setpoint_step(SetpointState prev, bool in_state_c, bool sample_valid,
                           float watts, uint32_t now_ms);

// "live", "held" or "zero".
const char *setpoint_status_name(SetpointStatus status);
```

- [ ] **Step 4: Write the implementation**

Create `lib/cp_setpoint/cp_setpoint.cpp`:

```cpp
#include "cp_setpoint.h"

namespace {

int32_t whole_watts(float watts) {
  return static_cast<int32_t>(watts + 0.5f);  // powers are never negative
}

}  // namespace

SetpointState setpoint_init() {
  return {false, false, 0.0f, 0};
}

SetpointStep setpoint_step(SetpointState prev, bool in_state_c, bool sample_valid,
                           float watts, uint32_t now_ms) {
  if (!in_state_c) {
    return {setpoint_init(), SetpointStatus::Zero, 0, false, 0};
  }

  if (sample_valid) {
    const SetpointState next = {true, false, watts, now_ms};
    return {next, SetpointStatus::Live, whole_watts(watts), true, 0};
  }

  if (!prev.have_valid) {
    return {prev, SetpointStatus::Zero, 0, false, 0};
  }

  const uint32_t age = now_ms - prev.last_valid_ms;
  if (!prev.expired && age <= kSetpointHoldMs) {
    return {prev, SetpointStatus::Held, whole_watts(prev.last_watts), true, age};
  }

  SetpointState expired = prev;
  expired.expired = true;
  return {expired, SetpointStatus::Zero, 0, true, age};
}

const char *setpoint_status_name(SetpointStatus status) {
  switch (status) {
    case SetpointStatus::Live:
      return "live";
    case SetpointStatus::Held:
      return "held";
    case SetpointStatus::Zero:
      return "zero";
  }
  return "zero";
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all suites PASS, including `test_cp_setpoint` (10 cases); total 74 test cases succeeded (64 existing + 10).

- [ ] **Step 6: Commit**

```bash
git add EVSim/lib/cp_setpoint EVSim/test/test_cp_setpoint
git commit -m "Add the pure held-setpoint rules

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `lib/cp_json` — one sample as JSON

**Files:**
- Create: `EVSim/lib/cp_json/cp_json.h`
- Create: `EVSim/lib/cp_json/cp_json.cpp`
- Test: `EVSim/test/test_cp_json/test_cp_json.cpp`

**Interfaces:**
- Produces (used by Task 4):
  - `struct CpJsonFields { uint32_t t_ms; float freq_hz; float duty_pct; int adc_raw; const char *cp; const char *state; float amps; float watts; int32_t setpoint_w; const char *status; bool age_known; uint32_t age_ms; };`
  - `int cp_json_format(char *buf, size_t size, const CpJsonFields &fields);` — returns the message length (excluding the NUL) or `-1` on a bad argument, an unsafe string, or a buffer that is too small.

- [ ] **Step 1: Write the failing test**

Create `test/test_cp_json/test_cp_json.cpp`:

```cpp
// Host-side tests for the CP telemetry JSON. Run with:
//   pio test -e native
#include <math.h>
#include <string.h>
#include <unity.h>

#include "cp_json.h"

static CpJsonFields sample() {
  CpJsonFields f;
  f.t_ms = 123456;
  f.freq_hz = 1000.0f;
  f.duty_pct = 24.3f;
  f.adc_raw = 0;
  f.cp = "PWM_VALID";
  f.state = "C";
  f.amps = 14.58f;
  f.watts = 10060.0f;
  f.setpoint_w = 10060;
  f.status = "live";
  f.age_known = true;
  f.age_ms = 0;
  return f;
}

static const char *kExpected =
    "{\"t\":123456,\"freq\":1000.0,\"duty\":24.30,\"adc\":0,\"cp\":\"PWM_VALID\","
    "\"state\":\"C\",\"amps\":14.58,\"watts\":10060,\"setpoint_w\":10060,"
    "\"status\":\"live\",\"age_ms\":0}";

void test_a_full_sample_formats_exactly_as_the_contract(void) {
  char buf[256];
  const int n = cp_json_format(buf, sizeof buf, sample());
  TEST_ASSERT_EQUAL_STRING(kExpected, buf);
  TEST_ASSERT_EQUAL_INT((int)strlen(kExpected), n);
}

void test_missing_current_and_power_are_null(void) {
  CpJsonFields f = sample();
  f.cp = "NO_SIGNAL";
  f.amps = NAN;
  f.watts = NAN;
  f.setpoint_w = 0;
  f.status = "zero";
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"amps\":null,\"watts\":null,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"setpoint_w\":0,\"status\":\"zero\""));
}

void test_an_unknown_age_is_null(void) {
  CpJsonFields f = sample();
  f.age_known = false;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"age_ms\":null}"));
}

void test_non_finite_frequency_and_duty_never_print_a_bare_token(void) {
  CpJsonFields f = sample();
  f.freq_hz = NAN;
  f.duty_pct = INFINITY;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"freq\":null,\"duty\":null,"));
  TEST_ASSERT_NULL(strstr(buf, "nan"));
  TEST_ASSERT_NULL(strstr(buf, "inf"));
}

void test_a_number_too_long_for_its_field_becomes_null_not_a_truncated_number(void) {
  CpJsonFields f = sample();
  f.freq_hz = 1e30f;  // "%.1f" would need 32 characters
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"freq\":null,"));
}

void test_numbers_round_to_their_decimals(void) {
  CpJsonFields f = sample();
  f.duty_pct = 24.999f;
  f.amps = 14.584f;
  f.watts = 10060.4f;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"duty\":25.00,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"amps\":14.58,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"watts\":10060,"));
}

void test_the_buffer_boundary_is_exact(void) {
  char big[256];
  const int n = cp_json_format(big, sizeof big, sample());
  TEST_ASSERT_TRUE(n > 0);

  char exact[256];
  TEST_ASSERT_EQUAL_INT(n, cp_json_format(exact, (size_t)n + 1, sample()));
  TEST_ASSERT_EQUAL_STRING(big, exact);

  char one_short[256];
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(one_short, (size_t)n, sample()));
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(one_short, 0, sample()));
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(nullptr, 256, sample()));
}

void test_text_that_would_break_the_json_is_rejected(void) {
  char buf[256];
  CpJsonFields f = sample();
  f.cp = "PWM\"VALID";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.state = "C\\";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.status = "li\nve";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.cp = nullptr;
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));
}

void test_the_worst_case_message_fits_in_256_bytes(void) {
  CpJsonFields f = sample();
  f.t_ms = 4294967295u;
  f.freq_hz = 99999.9f;
  f.duty_pct = 100.0f;
  f.adc_raw = 2147483647;
  f.cp = "OUT_OF_RANGE";
  f.amps = 99.99f;
  f.watts = 99999.0f;
  f.setpoint_w = 2147483647;
  f.status = "held";
  f.age_ms = 4294967295u;
  char buf[256];
  const int n = cp_json_format(buf, sizeof buf, f);
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_TRUE(n < 256);
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_full_sample_formats_exactly_as_the_contract);
  RUN_TEST(test_missing_current_and_power_are_null);
  RUN_TEST(test_an_unknown_age_is_null);
  RUN_TEST(test_non_finite_frequency_and_duty_never_print_a_bare_token);
  RUN_TEST(test_a_number_too_long_for_its_field_becomes_null_not_a_truncated_number);
  RUN_TEST(test_numbers_round_to_their_decimals);
  RUN_TEST(test_the_buffer_boundary_is_exact);
  RUN_TEST(test_text_that_would_break_the_json_is_rejected);
  RUN_TEST(test_the_worst_case_message_fits_in_256_bytes);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `~/.platformio/penv/bin/pio test -e native -f test_cp_json`
Expected: FAIL to build — `cp_json.h: No such file or directory`.

- [ ] **Step 3: Write the header**

Create `lib/cp_json/cp_json.h`:

```cpp
// Formats one CP sample, with its held setpoint, as the JSON message published
// on evsim/cp.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the
// host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md for the field
// contract.
#pragma once

#include <stddef.h>
#include <stdint.h>

struct CpJsonFields {
  uint32_t t_ms;        // board uptime, millis()
  float freq_hz;
  float duty_pct;
  int adc_raw;
  const char *cp;       // "NO_SIGNAL", "DC_12V", "PWM_VALID" or "OUT_OF_RANGE"
  const char *state;    // pilot state "A", "B" or "C"
  float amps;           // NaN -> null
  float watts;          // NaN -> null
  int32_t setpoint_w;
  const char *status;   // "live", "held" or "zero"
  bool age_known;       // false -> null
  uint32_t age_ms;
};

// Writes the message into `buf` and returns its length, excluding the NUL.
// Returns -1 -- writing nothing usable -- for a null or empty buffer, a buffer
// too small for the whole message (never a truncated message), or a string
// field that is null or holds a quote, a backslash or a byte outside printable
// ASCII, none of which the fixed names ever contain. A non-finite number, or
// one too long for its field, is written as null rather than as a bare
// nan/inf token or a silently shortened number.
int cp_json_format(char *buf, size_t size, const CpJsonFields &fields);
```

- [ ] **Step 4: Write the implementation**

Create `lib/cp_json/cp_json.cpp`:

```cpp
#include "cp_json.h"

#include <cmath>
#include <stdio.h>

namespace {

// `decimals` places, or "null" when the value is not finite or does not fit.
void number_or_null(char *out, size_t size, float value, int decimals) {
  if (!std::isfinite(value)) {
    snprintf(out, size, "null");
    return;
  }
  const int n = snprintf(out, size, "%.*f", decimals, static_cast<double>(value));
  if (n < 0 || static_cast<size_t>(n) >= size) {
    snprintf(out, size, "null");
  }
}

// True for a non-null string of printable ASCII with no quote or backslash.
bool plain_text(const char *s) {
  if (s == nullptr) {
    return false;
  }
  for (; *s != '\0'; s++) {
    const unsigned char c = static_cast<unsigned char>(*s);
    if (c < 0x20 || c > 0x7e || c == '"' || c == '\\') {
      return false;
    }
  }
  return true;
}

}  // namespace

int cp_json_format(char *buf, size_t size, const CpJsonFields &f) {
  if (buf == nullptr || size == 0 || !plain_text(f.cp) || !plain_text(f.state) ||
      !plain_text(f.status)) {
    return -1;
  }

  char freq[16], duty[16], amps[16], watts[16], age[16];
  number_or_null(freq, sizeof freq, f.freq_hz, 1);
  number_or_null(duty, sizeof duty, f.duty_pct, 2);
  number_or_null(amps, sizeof amps, f.amps, 2);
  number_or_null(watts, sizeof watts, f.watts, 0);
  if (f.age_known) {
    snprintf(age, sizeof age, "%lu", static_cast<unsigned long>(f.age_ms));
  } else {
    snprintf(age, sizeof age, "null");
  }

  const int n = snprintf(
      buf, size,
      "{\"t\":%lu,\"freq\":%s,\"duty\":%s,\"adc\":%d,\"cp\":\"%s\","
      "\"state\":\"%s\",\"amps\":%s,\"watts\":%s,\"setpoint_w\":%ld,"
      "\"status\":\"%s\",\"age_ms\":%s}",
      static_cast<unsigned long>(f.t_ms), freq, duty, f.adc_raw, f.cp, f.state,
      amps, watts, static_cast<long>(f.setpoint_w), f.status, age);
  if (n < 0 || static_cast<size_t>(n) >= size) {
    return -1;
  }
  return n;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all suites PASS, including `test_cp_json` (9 cases); total 83 test cases succeeded (74 + 9).

- [ ] **Step 6: Commit**

```bash
git add EVSim/lib/cp_json EVSim/test/test_cp_json
git commit -m "Add the pure JSON formatter for a CP sample

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The telemetry mailbox in `net_mqtt`

**Files:**
- Modify: `EVSim/src/mqtt_config.h`
- Modify: `EVSim/src/net_mqtt.h`
- Modify: `EVSim/src/net_mqtt.cpp`

**Interfaces:**
- Consumes: nothing new from earlier tasks (independent of Tasks 1 and 2).
- Produces (used by Task 4):
  - `constexpr size_t kMqttCpCapacity = 256;` (in `net_mqtt.h`)
  - `void net_mqtt_publish_cp(const char *json);` — copies `json` into a one-slot mailbox, replacing any unsent one; never blocks; a string of `kMqttCpCapacity` characters or more is refused.

- [ ] **Step 1: Add the topic to `src/mqtt_config.h`**

After the line `constexpr char kMqttTopicStatus[] = "evsim/status";`, add:

```cpp

// One JSON message per CP sample: the raw measurement plus the held setpoint.
// QoS 0, not retained; see the CP telemetry spec.
constexpr char kMqttTopicCp[] = "evsim/cp";
```

- [ ] **Step 2: Extend `src/net_mqtt.h`**

Change the invariant comment's last sentence so it names the mailbox. Replace

```
// runs in esp-mqtt's own task or in one small pump task; loop() talks to them
// only through two FreeRTOS queues, always with a timeout of 0. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md.
```

with

```
// runs in esp-mqtt's own task or in one small pump task; loop() talks to them
// only through FreeRTOS queues -- the control queue and a one-slot telemetry
// mailbox -- never waiting. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md and
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md.
```

Add `#include <stdint.h>` is not needed. After the `net_mqtt_publish_reply` declaration, add:

```cpp

// Longest telemetry message, terminator included.
constexpr size_t kMqttCpCapacity = 256;

// Hands one CP telemetry message (a JSON text) to be published on evsim/cp at
// QoS 0. It goes into a one-slot mailbox: an unsent older message is replaced,
// so the newest sample always wins and a slow network never queues stale data.
// Never blocks. A message of kMqttCpCapacity characters or more is refused
// rather than truncated. Nothing is sent while the client is disconnected.
void net_mqtt_publish_cp(const char *json);
```

- [ ] **Step 3: Add the mailbox to `src/net_mqtt.cpp`**

(a) After the `TxItem` struct, add:

```cpp

struct CpItem {
  char text[kMqttCpCapacity];
};
```

(b) Change `constexpr uint32_t kPumpPeriodMs = 100;` to:

```cpp
constexpr uint32_t kPumpPeriodMs = 20;  // short, so the telemetry mailbox is served promptly
```

(c) After `QueueHandle_t g_tx = nullptr;` add:

```cpp
QueueHandle_t g_cp = nullptr;  // one slot, written with xQueueOverwrite
```

(d) After `std::atomic<uint32_t> g_errors{0};` add:

```cpp
std::atomic<uint32_t> g_cp_replaced{0};  // unsent telemetry replaced by a newer sample
```

(e) In `pump_task`, directly after the closing brace of the `if (xQueueReceive(g_tx, ...) == pdTRUE) { ... }` block and before the `// An internal disconnect with WiFi still up ...` comment, add:

```cpp

    // Telemetry comes after the control queue, so a state change or reply is
    // never held up behind it. A QoS 0 publish is sent from this task and may
    // wait for the network; that delays only this task, never loop().
    CpItem cp;
    if (xQueueReceive(g_cp, &cp, 0) == pdTRUE && g_connected) {
      esp_mqtt_client_publish(g_client, kMqttTopicCp, cp.text, 0, 0, 0);
    }
```

(f) In `net_mqtt_begin()`, after `g_tx = xQueueCreate(kTxDepth, sizeof(TxItem));` add:

```cpp
  g_cp = xQueueCreate(1, sizeof(CpItem));
```

and in the failure condition change `if (g_rx == nullptr || g_tx == nullptr || g_client == nullptr ||` to:

```cpp
  if (g_rx == nullptr || g_tx == nullptr || g_cp == nullptr || g_client == nullptr ||
```

and in the failure branch, after `g_tx = nullptr;` add `g_cp = nullptr;`.

(g) After `net_mqtt_publish_reply`, add:

```cpp

void net_mqtt_publish_cp(const char *json) {
  if (g_cp == nullptr || json == nullptr || strlen(json) >= kMqttCpCapacity) {
    return;
  }
  CpItem item;
  memcpy(item.text, json, strlen(json) + 1);
  // The pump may take the old item between this check and the overwrite, which
  // only miscounts a replacement by one.
  if (uxQueueMessagesWaiting(g_cp) != 0) {
    g_cp_replaced++;
  }
  xQueueOverwrite(g_cp, &item);
}
```

(h) In `net_mqtt_print_status()`, replace the final two lines

```cpp
  Serial.print(", errors ");
  Serial.println(g_errors.load());
```

with

```cpp
  Serial.print(", errors ");
  Serial.print(g_errors.load());
  Serial.print(", cp replaced ");
  Serial.println(g_cp_replaced.load());
```

- [ ] **Step 4: Build both environments and run the native tests**

Run: `~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage && ~/.platformio/penv/bin/pio test -e native`
Expected: both builds `SUCCESS`; 83 test cases succeeded. (`net_mqtt_publish_cp` is not called yet, so the linker may drop it; the compile is the check.)

- [ ] **Step 5: Commit**

```bash
git add EVSim/src/mqtt_config.h EVSim/src/net_mqtt.h EVSim/src/net_mqtt.cpp
git commit -m "Add the one-slot telemetry mailbox to the MQTT pump

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Wire the telemetry into `main.cpp`

**Files:**
- Modify: `EVSim/src/main.cpp`

**Interfaces:**
- Consumes: `setpoint_init()`, `setpoint_step()`, `SetpointState`, `SetpointStep`, `setpoint_status_name()` from Task 1; `CpJsonFields`, `cp_json_format()` from Task 2; `kMqttCpCapacity`, `net_mqtt_publish_cp()` from Task 3.
- Produces: the finished firmware.

- [ ] **Step 1: Update the header comment and includes**

In `src/main.cpp`, replace the sentence in the file's top comment

```
// loop: no network condition may delay CP sampling. Publishing the CP
// measurements over MQTT comes later.
```

with

```
// loop: no network condition may delay CP sampling. Every sample is also
// published on evsim/cp as one JSON message with its held setpoint (see
// lib/cp_setpoint); the CSV below is unchanged.
```

and add to the includes, keeping alphabetical order:

```cpp
#include "cp_json.h"
```

after `#include "cp_convert.h"`, and

```cpp
#include "cp_setpoint.h"
```

after `#include "cp_reader.h"`.

- [ ] **Step 2: Add the setpoint state**

After `PilotState g_state = PilotState::A;` add:

```cpp

// The held-setpoint state (lib/cp_setpoint). Reset whenever the pilot state
// changes, so a held value can never outlive the state C it was measured in.
SetpointState g_setpoint = setpoint_init();
```

- [ ] **Step 3: Reset it on every pilot-state change**

In `handle_command`, directly after the line `g_state = result.state;`, add:

```cpp
  g_setpoint = setpoint_init();
```

- [ ] **Step 4: Publish in the sample tick**

At the end of `loop()`, after the line `Serial.println(pilot_state_name(g_state));` (the last CSV field), add:

```cpp

  // Telemetry after the CSV row, so nothing new can precede it. A sample is
  // valid only if it is a usable offer whose power is a number.
  const bool valid = state == CpState::PwmValid && !std::isnan(watts);
  const SetpointStep setpoint = setpoint_step(
      g_setpoint, g_state == PilotState::C, valid, watts, now);
  g_setpoint = setpoint.state;

  CpJsonFields fields;
  fields.t_ms = now;
  fields.freq_hz = sample.freq_hz;
  fields.duty_pct = sample.duty_pct;
  fields.adc_raw = sample.adc_raw;
  fields.cp = cp_state_name(state);
  fields.state = pilot_state_name(g_state);
  fields.amps = amps;
  fields.watts = watts;
  fields.setpoint_w = setpoint.setpoint_w;
  fields.status = setpoint_status_name(setpoint.status);
  fields.age_known = setpoint.age_known;
  fields.age_ms = setpoint.age_ms;

  char json[kMqttCpCapacity];
  if (cp_json_format(json, sizeof json, fields) > 0) {
    net_mqtt_publish_cp(json);
  }
```

- [ ] **Step 5: Build both environments and run the native tests**

Run: `~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage && ~/.platformio/penv/bin/pio test -e native`
Expected: both builds `SUCCESS`; 83 test cases succeeded. The serial output text is unchanged: no line was removed or reworded.

- [ ] **Step 6: Commit**

```bash
git add EVSim/src/main.cpp
git commit -m "Publish each CP sample with its held setpoint

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Bench acceptance (hardware, not for a subagent)

This task needs the board, the broker and, for two steps, the charger. It is run by the controller together with the human, after Tasks 1–4 are reviewed and committed. Flash the branch first: `cd EVSim && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage -t upload`. Start Mosquitto with `/opt/homebrew/sbin/mosquitto -c tools/mosquitto-lab.conf -v` and reset the board.

- [ ] **Step 1: The rate.** Run `timeout 10 mosquitto_sub -h 192.168.21.137 -t evsim/cp | wc -l`. Expected: 48–52 lines (5 per second).

- [ ] **Step 2: Every message is valid JSON with the contract's fields.** Run:

```bash
timeout 12 mosquitto_sub -h 192.168.21.137 -t evsim/cp | python3 -c '
import sys, json
keys = ["t","freq","duty","adc","cp","state","amps","watts","setpoint_w","status","age_ms"]
n = 0
for line in sys.stdin:
    m = json.loads(line)
    assert list(m) == keys, list(m)
    n += 1
print(n, "messages OK")'
```

Expected: `~55–60 messages OK` and no traceback. `null` values parse as `None`; no `NaN` token.

- [ ] **Step 3: Status follows the pilot state.** Watch `mosquitto_sub -h 192.168.21.137 -t evsim/cp -v` while publishing `A`, `B`, `C` to `evsim/cmd/state`. Expected: `state` and `status` change on the very next message; in `A` and `B` the status is `zero` and `setpoint_w` is 0.

- [ ] **Step 4: `live`, `held`, `zero` with the charger** (charger connected and signalling, board in state C): `status` is `live`, `setpoint_w` equals `watts`. Interrupt the CP signal (unplug the CP lead or switch the charger off) for about 10 s: `status` becomes `held`, `setpoint_w` stays at the last value and `age_ms` climbs from about 0 to about 10000. Keep it interrupted past 30 s: `status` becomes `zero` and stays `zero` until the signal returns, then `live` again on the first valid sample.

- [ ] **Step 5: Control traffic is not displaced.** With the stream running, send `B`, `A` and `B` back to back with `mosquitto_pub`. Expected on `evsim/#`: each command still gets its `evsim/reply` and a matching `evsim/state` within about a second.

- [ ] **Step 6: A dead broker does not touch sampling.** Stop Mosquitto for 30 s while a serial capture runs (`~/.platformio/penv/bin/python tools/cp_jitter.py capture ...` needs the reset script from earlier sessions, or use the monitor): the CSV rows must keep arriving every 200 ms with no gap. Restart the broker: `evsim/cp` resumes within about 5 s. `W` shows `cp replaced` small or zero.

- [ ] **Step 7: Jitter.** In the build actually used on the bench, record about 60 s with the telemetry stream running and about 60 s with the broker stopped, in the same pilot state (C), plus a baseline capture from the merged master firmware without this feature if available. Compare the duty spread (standard deviation) and the lag-1 autocorrelation, as done for the state-command feature with `tools/cp_jitter.py` captures (`data/mqtt_baseline.csv` vs `data/mqtt_on.csv` were the previous ones). Note that in the low-voltage build (`env:esp32dev-lowvoltage`) the ADC sampling task (`src/cp_reader_adc.cpp`) is pinned to core 0 at priority 1, the same core and priority as the MQTT pump, and the reported frequency is hard-coded to 1000 Hz, so the frequency is a constant there and proves nothing. The duty spread between two captures of identical firmware already differs by about 1 point, so only a clear shift (several points) or a change in the autocorrelation structure is evidence of disturbance.

---

## Self-Review

**Spec coverage:** the message contract → Task 2 (format) and Task 4 (fields filled from the sample tick); the three status rules and the 30 s window → Task 1; "leaving C forgets the value" → Task 1 test plus the reset in Task 4 step 3; mailbox, pump order, 20 ms wake-up, replacement counter in `W` → Task 3; QoS 0 not retained → Task 3 step 3(e); no averaging on the board → nothing in the plan filters; CSV unchanged → Task 4 adds after the CSV row only; failure handling (link down drops samples, buffer too small skips) → Task 3 (publishes only when connected) and Task 4 (`> 0` check); testing section → Tasks 1, 2 and 5.

**Placeholder scan:** no TBD/TODO; every code step has its code, every edit to an existing file names its anchor.

**Type consistency:** `SetpointStep` fields (`state`, `status`, `setpoint_w`, `age_known`, `age_ms`) are used identically in the tests, the implementation and `main.cpp`; `CpJsonFields` has the same twelve members in the header, the tests and `main.cpp`; `kMqttCpCapacity` (256) is used for both the mailbox item and the `json` buffer in `main.cpp`, so a message that `cp_json_format` accepts always fits `net_mqtt_publish_cp`; `net_mqtt_publish_cp(const char *)` matches between header, implementation and call.

**Known limits, stated rather than hidden:** the QoS 0 publish is sent from the pump task and can wait for the network when a link is dead but undetected (about 15 s); state publishes queued behind it wait. The pump now wakes 50 times per second instead of 10. The publish rate under load (many state commands during the stream) is verified on the bench, not in a unit test.
