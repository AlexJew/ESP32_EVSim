# Pilot State Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive GPIO33 and GPIO32 to present IEC 61851 pilot states A, B and C to the charger, commanded by typing a letter over serial.

**Architecture:** The rules (transition table, pin mapping, safe write order, command parsing) live in a pure `lib/pilot_state/` library with no Arduino dependency, so they are unit-tested on the host. A thin `src/pilot_driver.*` owns the two GPIOs and executes the write plan. `src/main.cpp` gains a serial line reader and a `commanded` CSV column. The existing CP PWM reader is untouched.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev`, Unity tests on the `native` env. `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. All commands below run from the `EVSim/` directory.

**Spec:** `docs/superpowers/specs/2026-09-21-pilot-state-control-design.md`

## Global Constraints

- GPIO33 drives Q1 (2k7, state B). GPIO32 drives Q2 (1k2, state C).
- State pins: A = 33 low / 32 low; B = 33 high / 32 low; C = 33 high / 32 high.
- The only refused transition is **A → C**. Every other transition is accepted; requesting the current state is a no-op.
- GPIO32 must never be high while GPIO33 is low. Going up sets 33 then 32; going down clears 32 then 33.
- `lib/pilot_state/` has no Arduino dependency and must compile on both `esp32dev` and `native`.
- The state is always A after reset. `pilot_driver_begin()` writes both pins low before switching them to outputs.
- Serial is 115200 baud, line-based. Every reply is `#`-prefixed so the CSV stays parseable.
- CSV header becomes `millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw,commanded`; existing columns do not move.
- Do not modify `src/cp_reader.*` or `lib/cp_convert/`.
- `EVSim/.vscode/c_cpp_properties.json` has unrelated uncommitted changes. Never use `git add -A` or `git add .`; add only the files named in each task.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `lib/pilot_state/pilot_state.h` | Types and function declarations for the pilot rules |
| `lib/pilot_state/pilot_state.cpp` | Transition table, pin mapping, write plan, command parser, state names |
| `test/test_pilot_state/test_pilot_state.cpp` | Host-side Unity tests for all of the above |
| `src/pilot_driver.h`, `src/pilot_driver.cpp` | Owns GPIO32/GPIO33; applies a write plan |
| `src/main.cpp` | Serial line reader, command handling, `commanded` CSV column |

---

### Task 1: State, transition table and pin mapping

**Files:**
- Create: `lib/pilot_state/pilot_state.h`
- Create: `lib/pilot_state/pilot_state.cpp`
- Create: `test/test_pilot_state/test_pilot_state.cpp`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `enum class PilotState { A, B, C }`
  - `struct PilotResult { bool accepted; PilotState state; }`
  - `struct PilotPins { bool b_high; bool c_high; }`
  - `PilotResult pilot_request(PilotState current, PilotState requested)`. A refused request returns `{false, current}`. An accepted or no-op request returns `{true, requested}`.
  - `PilotPins pilot_pins(PilotState state)`
  - `const char *pilot_state_name(PilotState state)`

- [ ] **Step 1: Write the failing tests**

Create `test/test_pilot_state/test_pilot_state.cpp`:

```cpp
// Host-side tests for the pilot state rules. Run with:
//   pio test -e native
#include <unity.h>

#include "pilot_state.h"

static const PilotState kAll[] = {PilotState::A, PilotState::B, PilotState::C};

// --- transition table ------------------------------------------------------

void test_every_transition_except_a_to_c_is_accepted(void) {
  for (PilotState from : kAll) {
    for (PilotState to : kAll) {
      const PilotResult r = pilot_request(from, to);
      const bool expect_accepted = !(from == PilotState::A && to == PilotState::C);
      TEST_ASSERT_TRUE(r.accepted == expect_accepted);
      if (expect_accepted) {
        TEST_ASSERT_EQUAL_INT((int)to, (int)r.state);
      }
    }
  }
}

void test_a_to_c_is_refused_and_leaves_the_state_unchanged(void) {
  const PilotResult r = pilot_request(PilotState::A, PilotState::C);
  TEST_ASSERT_FALSE(r.accepted);
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)r.state);
  // Pins for the unchanged state are still both low.
  const PilotPins pins = pilot_pins(r.state);
  TEST_ASSERT_FALSE(pins.b_high);
  TEST_ASSERT_FALSE(pins.c_high);
}

void test_requesting_the_current_state_is_accepted_and_unchanged(void) {
  for (PilotState s : kAll) {
    const PilotResult r = pilot_request(s, s);
    TEST_ASSERT_TRUE(r.accepted);
    TEST_ASSERT_EQUAL_INT((int)s, (int)r.state);
  }
}

// --- pin mapping -----------------------------------------------------------

void test_state_a_drives_both_pins_low(void) {
  const PilotPins p = pilot_pins(PilotState::A);
  TEST_ASSERT_FALSE(p.b_high);
  TEST_ASSERT_FALSE(p.c_high);
}

void test_state_b_drives_only_the_b_pin(void) {
  const PilotPins p = pilot_pins(PilotState::B);
  TEST_ASSERT_TRUE(p.b_high);
  TEST_ASSERT_FALSE(p.c_high);
}

void test_state_c_drives_both_pins_high(void) {
  // 1k2 in parallel with 2k7: the B leg stays in circuit.
  const PilotPins p = pilot_pins(PilotState::C);
  TEST_ASSERT_TRUE(p.b_high);
  TEST_ASSERT_TRUE(p.c_high);
}

void test_no_state_asserts_c_without_b(void) {
  for (PilotState s : kAll) {
    const PilotPins p = pilot_pins(s);
    TEST_ASSERT_FALSE(p.c_high && !p.b_high);
  }
}

// --- names -----------------------------------------------------------------

void test_state_names(void) {
  TEST_ASSERT_EQUAL_STRING("A", pilot_state_name(PilotState::A));
  TEST_ASSERT_EQUAL_STRING("B", pilot_state_name(PilotState::B));
  TEST_ASSERT_EQUAL_STRING("C", pilot_state_name(PilotState::C));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_every_transition_except_a_to_c_is_accepted);
  RUN_TEST(test_a_to_c_is_refused_and_leaves_the_state_unchanged);
  RUN_TEST(test_requesting_the_current_state_is_accepted_and_unchanged);
  RUN_TEST(test_state_a_drives_both_pins_low);
  RUN_TEST(test_state_b_drives_only_the_b_pin);
  RUN_TEST(test_state_c_drives_both_pins_high);
  RUN_TEST(test_no_state_asserts_c_without_b);
  RUN_TEST(test_state_names);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `~/.platformio/penv/bin/pio test -e native -f test_pilot_state`
Expected: build FAILS with `pilot_state.h: No such file or directory`.

- [ ] **Step 3: Write the minimal implementation**

Create `lib/pilot_state/pilot_state.h`:

```cpp
// Pilot state rules: which of the IEC 61851 states A, B, C the board may present
// to the charger, and which GPIO levels produce each one.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the host
// as well as on the ESP32. See
// docs/superpowers/specs/2026-09-21-pilot-state-control-design.md.
#pragma once

// A: no vehicle. B: connected, not ready (2k7 on CP). C: connected, ready (1k2
// in parallel with 2k7, so the B leg stays in circuit).
enum class PilotState { A, B, C };

struct PilotResult {
  bool accepted;    // false only when the request was refused
  PilotState state; // the resulting state; unchanged when refused
};

// Levels for the two MOSFET gates. b_high is GPIO33 (Q1, 2k7); c_high is GPIO32
// (Q2, 1k2).
struct PilotPins {
  bool b_high;
  bool c_high;
};

// Applies the transition table. The only refused request is A -> C, because B
// has not been commanded. Requesting the current state is accepted and leaves
// it unchanged; the caller tells a no-op apart by comparing states.
PilotResult pilot_request(PilotState current, PilotState requested);

// Gate levels that present `state`.
PilotPins pilot_pins(PilotState state);

// Stable short name for logging: "A", "B" or "C".
const char *pilot_state_name(PilotState state);
```

Create `lib/pilot_state/pilot_state.cpp`:

```cpp
#include "pilot_state.h"

PilotResult pilot_request(PilotState current, PilotState requested) {
  // C is 1k2 in parallel with the 2k7 that B asserts, never a replacement for
  // it, so C is only reachable once B has been commanded.
  if (current == PilotState::A && requested == PilotState::C) {
    return {false, current};
  }
  return {true, requested};
}

PilotPins pilot_pins(PilotState state) {
  switch (state) {
    case PilotState::A:
      return {false, false};
    case PilotState::B:
      return {true, false};
    case PilotState::C:
      return {true, true};
  }
  return {false, false};  // unreachable; state A is the safe default
}

const char *pilot_state_name(PilotState state) {
  switch (state) {
    case PilotState::A:
      return "A";
    case PilotState::B:
      return "B";
    case PilotState::C:
      return "C";
  }
  return "?";
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native -f test_pilot_state`
Expected: `8 Tests 0 Failures 0 Ignored` and `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add lib/pilot_state test/test_pilot_state
git commit -m "Add pilot state rules and pin mapping

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Safe pin write plan

**Files:**
- Modify: `lib/pilot_state/pilot_state.h`
- Modify: `lib/pilot_state/pilot_state.cpp`
- Modify: `test/test_pilot_state/test_pilot_state.cpp`

**Interfaces:**
- Consumes: `PilotState`, `PilotPins`, `pilot_pins()` from Task 1.
- Produces:
  - `enum class PilotPin { B, C }`. B is GPIO33/Q1, C is GPIO32/Q2.
  - `struct PilotWrite { PilotPin pin; bool high; }`
  - `constexpr int kPilotMaxWrites = 2`
  - `int pilot_plan(PilotState from, PilotState to, PilotWrite out[kPilotMaxWrites])`. It writes only the pins whose level changes, in an order that never leaves C high with B low, and returns the count (0 for a no-op).

- [ ] **Step 1: Write the failing tests**

In `test/test_pilot_state/test_pilot_state.cpp`, add this block immediately before `void setUp(void) {}`:

```cpp
// --- write plan ------------------------------------------------------------

static void apply_write(PilotPins &pins, const PilotWrite &w) {
  if (w.pin == PilotPin::B) {
    pins.b_high = w.high;
  } else {
    pins.c_high = w.high;
  }
}

void test_plan_never_leaves_c_high_without_b(void) {
  for (PilotState from : kAll) {
    for (PilotState to : kAll) {
      PilotPins pins = pilot_pins(from);
      PilotWrite writes[kPilotMaxWrites];
      const int n = pilot_plan(from, to, writes);
      TEST_ASSERT_TRUE(n >= 0 && n <= kPilotMaxWrites);
      for (int i = 0; i < n; i++) {
        apply_write(pins, writes[i]);
        TEST_ASSERT_FALSE(pins.c_high && !pins.b_high);
      }
      // The plan ends in exactly the pin levels of the target state.
      TEST_ASSERT_TRUE(pins.b_high == pilot_pins(to).b_high);
      TEST_ASSERT_TRUE(pins.c_high == pilot_pins(to).c_high);
    }
  }
}

void test_plan_for_the_same_state_writes_nothing(void) {
  for (PilotState s : kAll) {
    PilotWrite writes[kPilotMaxWrites];
    TEST_ASSERT_EQUAL_INT(0, pilot_plan(s, s, writes));
  }
}

void test_plan_going_up_sets_b_before_c(void) {
  PilotWrite writes[kPilotMaxWrites];
  TEST_ASSERT_EQUAL_INT(2, pilot_plan(PilotState::A, PilotState::C, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[1].pin);
  TEST_ASSERT_TRUE(writes[1].high);
}

void test_plan_going_down_clears_c_before_b(void) {
  PilotWrite writes[kPilotMaxWrites];
  TEST_ASSERT_EQUAL_INT(2, pilot_plan(PilotState::C, PilotState::A, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[1].pin);
  TEST_ASSERT_FALSE(writes[1].high);
}

void test_plan_touches_only_the_pin_that_changes(void) {
  PilotWrite writes[kPilotMaxWrites];

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::A, PilotState::B, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::B, PilotState::C, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::C, PilotState::B, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::B, PilotState::A, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);
}

```

Then add these lines inside `main`, after `RUN_TEST(test_state_names);`:

```cpp
  RUN_TEST(test_plan_never_leaves_c_high_without_b);
  RUN_TEST(test_plan_for_the_same_state_writes_nothing);
  RUN_TEST(test_plan_going_up_sets_b_before_c);
  RUN_TEST(test_plan_going_down_clears_c_before_b);
  RUN_TEST(test_plan_touches_only_the_pin_that_changes);
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `~/.platformio/penv/bin/pio test -e native -f test_pilot_state`
Expected: build FAILS with `'PilotPin' has not been declared` (or `pilot_plan` not declared).

- [ ] **Step 3: Write the minimal implementation**

In `lib/pilot_state/pilot_state.h`, append:

```cpp

// One gate to drive. B is GPIO33 (Q1, 2k7); C is GPIO32 (Q2, 1k2).
enum class PilotPin { B, C };

struct PilotWrite {
  PilotPin pin;
  bool high;
};

// The most writes any transition needs.
constexpr int kPilotMaxWrites = 2;

// The ordered writes that take the board from `from` to `to`, touching only the
// pins whose level changes. The order never leaves C high while B is low, even
// briefly: going up sets B then C, going down clears C then B. Returns the
// number of writes placed in `out` (0 when the state does not change).
int pilot_plan(PilotState from, PilotState to, PilotWrite out[kPilotMaxWrites]);
```

In `lib/pilot_state/pilot_state.cpp`, append:

```cpp

int pilot_plan(PilotState from, PilotState to, PilotWrite out[kPilotMaxWrites]) {
  const PilotPins now = pilot_pins(from);
  const PilotPins next = pilot_pins(to);
  const bool going_up = static_cast<int>(to) > static_cast<int>(from);

  int n = 0;
  if (going_up) {
    if (next.b_high != now.b_high) out[n++] = {PilotPin::B, next.b_high};
    if (next.c_high != now.c_high) out[n++] = {PilotPin::C, next.c_high};
  } else {
    if (next.c_high != now.c_high) out[n++] = {PilotPin::C, next.c_high};
    if (next.b_high != now.b_high) out[n++] = {PilotPin::B, next.b_high};
  }
  return n;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native -f test_pilot_state`
Expected: `13 Tests 0 Failures 0 Ignored` and `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add lib/pilot_state test/test_pilot_state
git commit -m "Order pilot pin writes so C is never high without B

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Command parser

**Files:**
- Modify: `lib/pilot_state/pilot_state.h`
- Modify: `lib/pilot_state/pilot_state.cpp`
- Modify: `test/test_pilot_state/test_pilot_state.cpp`

**Interfaces:**
- Consumes: `PilotState` from Task 1.
- Produces: `bool pilot_parse_command(const char *line, PilotState *out)`. Accepts exactly one letter `A`, `B` or `C` (either case) with optional surrounding whitespace (space, tab, CR, LF). Returns false and leaves `*out` untouched for anything else, including null pointers, empty or blank lines, and more than one letter.

- [ ] **Step 1: Write the failing tests**

In `test/test_pilot_state/test_pilot_state.cpp`, add this block immediately before `void setUp(void) {}`:

```cpp
// --- command parsing -------------------------------------------------------

void test_parse_accepts_a_b_c_in_either_case(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_TRUE(pilot_parse_command("A", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("b", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("C", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::C, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("a", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)s);
}

void test_parse_ignores_surrounding_whitespace_and_line_endings(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_TRUE(pilot_parse_command(" c\r\n", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::C, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("\tB  ", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
}

void test_parse_rejects_anything_else(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_FALSE(pilot_parse_command("x", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("1", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("   ", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("AB", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("A B", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", &s));
}

void test_parse_leaves_the_output_untouched_on_rejection(void) {
  PilotState s = PilotState::B;
  TEST_ASSERT_FALSE(pilot_parse_command("x", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
}

void test_parse_rejects_null_pointers(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_FALSE(pilot_parse_command(nullptr, &s));
  TEST_ASSERT_FALSE(pilot_parse_command("A", nullptr));
}

```

Then add these lines inside `main`, after `RUN_TEST(test_plan_touches_only_the_pin_that_changes);`:

```cpp
  RUN_TEST(test_parse_accepts_a_b_c_in_either_case);
  RUN_TEST(test_parse_ignores_surrounding_whitespace_and_line_endings);
  RUN_TEST(test_parse_rejects_anything_else);
  RUN_TEST(test_parse_leaves_the_output_untouched_on_rejection);
  RUN_TEST(test_parse_rejects_null_pointers);
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `~/.platformio/penv/bin/pio test -e native -f test_pilot_state`
Expected: build FAILS with `'pilot_parse_command' was not declared in this scope`.

- [ ] **Step 3: Write the minimal implementation**

In `lib/pilot_state/pilot_state.h`, append:

```cpp

// Parses a serial command line: exactly one letter A, B or C (either case),
// optionally surrounded by spaces, tabs or line endings. Returns false and
// leaves *out untouched for anything else, including a null pointer, an empty
// or blank line, or more than one letter.
bool pilot_parse_command(const char *line, PilotState *out);
```

In `lib/pilot_state/pilot_state.cpp`, append:

```cpp

namespace {

bool is_blank(char c) {
  return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

}  // namespace

bool pilot_parse_command(const char *line, PilotState *out) {
  if (line == nullptr || out == nullptr) {
    return false;
  }

  while (is_blank(*line)) {
    line++;
  }
  const char letter = *line;
  if (letter == '\0') {
    return false;
  }
  line++;

  while (is_blank(*line)) {
    line++;
  }
  if (*line != '\0') {
    return false;  // a second letter or stray characters: not a command
  }

  switch (letter) {
    case 'A':
    case 'a':
      *out = PilotState::A;
      return true;
    case 'B':
    case 'b':
      *out = PilotState::B;
      return true;
    case 'C':
    case 'c':
      *out = PilotState::C;
      return true;
  }
  return false;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: both suites run. `test_pilot_state`: `18 Tests 0 Failures 0 Ignored`, and `test_cp_convert` still passes.

- [ ] **Step 5: Commit**

```bash
git add lib/pilot_state test/test_pilot_state
git commit -m "Parse single-letter pilot state commands

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: GPIO driver and serial command handling

**Files:**
- Create: `src/pilot_driver.h`
- Create: `src/pilot_driver.cpp`
- Modify: `src/main.cpp` (replace the whole file)

**Interfaces:**
- Consumes: `PilotState`, `PilotResult`, `PilotPin`, `PilotWrite`, `kPilotMaxWrites`, `pilot_request()`, `pilot_plan()`, `pilot_parse_command()`, `pilot_state_name()` from Tasks 1–3, and the existing `cp_reader_*` / `cp_*` functions.
- Produces: `kPilotPinB = 33`, `kPilotPinC = 32`, `void pilot_driver_begin()`, `void pilot_driver_apply(PilotState from, PilotState to)`. The firmware accepts `A`/`B`/`C` over serial and emits the `commanded` CSV column.

The GPIO layer cannot be unit-tested on the host, so this task is verified by a firmware build and then on hardware.

- [ ] **Step 1: Write the driver**

Create `src/pilot_driver.h`:

```cpp
// Drives the two MOSFET gates that switch the pilot resistors onto CP.
//
// Owns GPIO32 and GPIO33 and holds no rules of its own: which writes to make,
// and in what order, comes from pilot_plan() in lib/pilot_state.
#pragma once

#include <Arduino.h>

#include "pilot_state.h"

constexpr uint8_t kPilotPinB = 33;  // Q1, 2k7: state B
constexpr uint8_t kPilotPinC = 32;  // Q2, 1k2 in parallel with 2k7: state C

// Drives both pins low, then makes them outputs, leaving the board in state A.
void pilot_driver_begin();

// Executes the ordered writes that take the board from `from` to `to`.
void pilot_driver_apply(PilotState from, PilotState to);
```

Create `src/pilot_driver.cpp`:

```cpp
#include "pilot_driver.h"

namespace {

uint8_t gpio_for(PilotPin pin) {
  return pin == PilotPin::B ? kPilotPinB : kPilotPinC;
}

}  // namespace

void pilot_driver_begin() {
  // The 100k pulldowns (R8/R9) already hold both gates low through reset. Write
  // the level before switching to output so the pins never glitch high.
  digitalWrite(kPilotPinB, LOW);
  digitalWrite(kPilotPinC, LOW);
  pinMode(kPilotPinB, OUTPUT);
  pinMode(kPilotPinC, OUTPUT);
}

void pilot_driver_apply(PilotState from, PilotState to) {
  PilotWrite writes[kPilotMaxWrites];
  const int n = pilot_plan(from, to, writes);
  for (int i = 0; i < n; i++) {
    digitalWrite(gpio_for(writes[i].pin), writes[i].high ? HIGH : LOW);
  }
}
```

- [ ] **Step 2: Replace `src/main.cpp`**

Overwrite `src/main.cpp` with:

```cpp
// EVSim -- reads the Control Pilot PWM the MENNEKES AMTRON signals on CP and
// reports the current and power it corresponds to, and drives the pilot state
// (A, B or C) the board presents to the charger.
//
// The state is commanded over serial: send A, B or C followed by a newline.
// GPIO32 (state C) is only ever asserted while GPIO33 (state B) is, so A -> C
// is refused. The state is always A after reset. Publishing over MQTT, and
// taking commands from it, come later.
//
// Output is CSV sharing column names with tools/charger_poll.py, so the two
// logs can be merged on time to produce the calibration curve. Replies to
// commands are '#'-prefixed lines, so the CSV stays parseable.

#include <Arduino.h>
#include <cmath>

#include "cp_convert.h"
#include "cp_reader.h"
#include "pilot_driver.h"
#include "pilot_state.h"

namespace {

constexpr uint32_t kSampleIntervalMs = 200;
uint32_t g_next_sample_ms = 0;

// The state the board is commanded to present. Always A after reset.
PilotState g_state = PilotState::A;

constexpr size_t kLineMax = 16;
char g_line[kLineMax];
size_t g_line_len = 0;
bool g_line_overflow = false;

void print_float_or_blank(float value, int decimals) {
  if (std::isnan(value)) {
    return;  // empty CSV cell: no value is invented for an invalid state
  }
  Serial.print(value, decimals);
}

void handle_command(const char *line) {
  PilotState requested = PilotState::A;
  if (!pilot_parse_command(line, &requested)) {
    Serial.print("# unknown command '");
    Serial.print(line);
    Serial.println("'");
    return;
  }

  if (requested == g_state) {
    Serial.print("# already in ");
    Serial.println(pilot_state_name(g_state));
    return;
  }

  const PilotResult result = pilot_request(g_state, requested);
  if (!result.accepted) {
    Serial.print("# refused ");
    Serial.print(pilot_state_name(g_state));
    Serial.print(" -> ");
    Serial.print(pilot_state_name(requested));
    Serial.println(": state B must be asserted first");
    return;
  }

  pilot_driver_apply(g_state, result.state);
  Serial.print("# state ");
  Serial.print(pilot_state_name(g_state));
  Serial.print(" -> ");
  Serial.println(pilot_state_name(result.state));
  g_state = result.state;
}

// Collects serial bytes into lines and hands each complete line to
// handle_command(). Either CR or LF ends a line, so CRLF terminals work; the
// empty line that CRLF produces is ignored. An overlong line is discarded.
void poll_serial() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());

    if (c == '\n' || c == '\r') {
      if (g_line_overflow) {
        Serial.println("# unknown command (line too long)");
      } else if (g_line_len > 0) {
        g_line[g_line_len] = '\0';
        handle_command(g_line);
      }
      g_line_len = 0;
      g_line_overflow = false;
      continue;
    }

    if (g_line_len < kLineMax - 1) {
      g_line[g_line_len++] = c;
    } else {
      g_line_overflow = true;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) {
    ;  // brief grace period for USB CDC, but never block a headless boot
  }

  pilot_driver_begin();
  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim -- CP PWM reader on GPIO35, pilot state control on GPIO32/33");
  Serial.println("# send A, B or C to set the pilot state; A -> C is refused");
  Serial.print("# pilot state ");
  Serial.println(pilot_state_name(g_state));
  Serial.println("millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw,commanded");
  g_next_sample_ms = millis() + kSampleIntervalMs;
}

void loop() {
  poll_serial();

  const uint32_t now = millis();
  if (static_cast<int32_t>(now - g_next_sample_ms) < 0) {
    return;
  }
  g_next_sample_ms = now + kSampleIntervalMs;

  const CpSample sample = cp_reader_sample();
  const CpState state = cp_classify(sample.duty_pct, sample.freq_hz,
                                    sample.level_high, sample.edges > 0);

  float amps = NAN;
  float watts = NAN;
  if (state == CpState::PwmValid) {
    amps = cp_duty_to_current(sample.duty_pct);
    watts = cp_current_to_power(amps);
  }

  Serial.print(now);
  Serial.print(',');
  Serial.print(sample.freq_hz, 1);
  Serial.print(',');
  Serial.print(sample.duty_pct, 2);
  Serial.print(',');
  print_float_or_blank(amps, 2);
  Serial.print(',');
  print_float_or_blank(watts, 0);
  Serial.print(',');
  Serial.print(cp_state_name(state));
  Serial.print(',');
  Serial.print(sample.adc_raw);
  Serial.print(',');
  Serial.println(pilot_state_name(g_state));
}
```

- [ ] **Step 3: Build the firmware and re-run the host tests**

Run: `~/.platformio/penv/bin/pio run -e esp32dev`
Expected: `SUCCESS`, no errors.

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: both suites pass (`test_pilot_state` 18 tests, `test_cp_convert` unchanged).

- [ ] **Step 4: Commit**

```bash
git add src/pilot_driver.h src/pilot_driver.cpp src/main.cpp
git commit -m "Drive the pilot state from serial commands

GPIO33/GPIO32 present states A, B and C. C is only reachable after B, and the
commanded state is logged next to the measured CP state.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Validate on hardware (needs the board and charger; run by the user)**

Flash with `~/.platformio/penv/bin/pio run -e esp32dev -t upload`, open the serial monitor at 115200 baud, and run `tools/charger_poll.py` alongside it. Step through:

| # | Send | Expect on the ESP32 | Expect on the charger (`Type2StateConnector1_vehicleif`) |
|---|---|---|---|
| 1 | (reset) | `# pilot state A`, CP reads `DC_12V`, `commanded` = A | no vehicle |
| 2 | `B` | `# state A -> B`, valid PWM appears on GPIO35 | `(B)` vehicle connected |
| 3 | `C` | `# state B -> C`, `adc_raw` reflects the ~2.9 V state C level | `(C)` vehicle connected ready |
| 4 | `A` | `# state C -> A`, both pins drop | no vehicle |
| 5 | `C` | `# refused A -> C: state B must be asserted first`, no change | unchanged |
| 6 | `x` | `# unknown command 'x'` | unchanged |

Record the `adc_raw` reading from step 3. It settles the open question about the state C level margin (~2.9 V against the ~2.48 V input-high threshold) from the CP PWM reader spec.
