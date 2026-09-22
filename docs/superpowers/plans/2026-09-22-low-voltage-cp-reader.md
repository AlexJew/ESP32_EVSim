# Low-Voltage CP Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an alternate CP PWM reader that reads GPIO35 via the ADC instead of the digital input, selected at flash time, for the interim bench setup where CP's HIGH level (~1.45 V) falls inside the ESP32's undefined digital-input band.

**Architecture:** A pure hysteresis function in `lib/cp_adc_level/` decides high/low from a raw ADC reading (host-tested). A FreeRTOS task in a new `src/cp_reader_adc.cpp`, pinned to core 0, samples the ADC and accumulates high-time exactly the way the existing ISR does, behind the same `cp_reader.h` interface. The existing reader is renamed to `cp_reader_isr.cpp`, untouched otherwise. A new PlatformIO environment picks which one gets compiled; `main.cpp` never changes.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev`, Unity tests on the `native` env, FreeRTOS tasks (arduino-esp32 core). `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. All commands below run from the `EVSim/` directory.

**Spec:** `docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md`

## Global Constraints

- Bench-observed signal: HIGH ≈ 1.45 V, LOW ≈ 0 V and clean.
- ADC thresholds: `kAdcHighThreshold = 900` (~0.73 V), `kAdcLowThreshold = 300` (~0.24 V), assuming `ADC_11db` attenuation's ~0–3.3 V over 0–4095 counts. Named constants, not literals.
- Hysteresis rule: goes high once `adc_raw >= high_thresh`, goes low once `adc_raw <= low_thresh`, otherwise holds `previous_level`.
- Do not modify `lib/cp_convert/`, `lib/pilot_state/`, `src/pilot_driver.*`, or `src/main.cpp`. `src/cp_reader.cpp` is renamed to `src/cp_reader_isr.cpp` with no content change.
- Selection is via `platformio.ini`'s `build_src_filter`: `[env:esp32dev]` excludes `cp_reader_adc.cpp`; a new `[env:esp32dev-lowvoltage]` (`extends = env:esp32dev`) excludes `cp_reader_isr.cpp` instead. Both compile the same `cp_reader.h` and the same `main.cpp`.
- Sampling: burst of `kSamplesPerBurst = 20` back-to-back `analogRead()` calls, then `vTaskDelay(1)`. Task pinned to core 0, 2048-byte stack, priority 1.
- Only the sampling task calls `analogRead(kCpPin)`. `cp_reader_sample()` reads the task's last captured raw value under the same lock; it never samples again itself.
- Use a `portMUX_TYPE` spinlock (`portENTER_CRITICAL`/`portEXIT_CRITICAL`) between the task and `cp_reader_sample()` — not `noInterrupts()`/`interrupts()`, which only guard true ISRs.
- Task creation failure: print `# cp_reader_adc: task creation failed` and leave the accumulators at zero, so the reader reports `NO_SIGNAL` forever rather than inventing a value.
- `EVSim/.vscode/c_cpp_properties.json` may carry unrelated local edits at any point. Never use `git add -A` or `git add .`; add only the files named in each task.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `lib/cp_adc_level/cp_adc_level.h` / `.cpp` | Pure hysteresis: raw ADC counts → high/low |
| `test/test_cp_adc_level/test_cp_adc_level.cpp` | Host-side Unity tests for the hysteresis |
| `src/cp_reader_isr.cpp` | The existing interrupt-based reader, renamed, unmodified |
| `src/cp_reader_adc.cpp` | New: FreeRTOS-task ADC sampling, same `cp_reader.h` interface |
| `EVSim/platformio.ini` | New `[env:esp32dev-lowvoltage]`; `build_src_filter` picks the reader |

---

### Task 1: ADC hysteresis logic

**Files:**
- Create: `lib/cp_adc_level/cp_adc_level.h`
- Create: `lib/cp_adc_level/cp_adc_level.cpp`
- Create: `test/test_cp_adc_level/test_cp_adc_level.cpp`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `constexpr int kAdcLowThreshold = 300;`
  - `constexpr int kAdcHighThreshold = 900;`
  - `bool cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh)`

- [ ] **Step 1: Write the failing tests**

Create `test/test_cp_adc_level/test_cp_adc_level.cpp`:

```cpp
// Host-side tests for the ADC level hysteresis. Run with:
//   pio test -e native
#include <unity.h>

#include "cp_adc_level.h"

// --- hysteresis --------------------------------------------------------

void test_well_below_low_threshold_is_low_regardless_of_previous_level(void) {
  const int raw = kAdcLowThreshold - 100;
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_well_above_high_threshold_is_high_regardless_of_previous_level(void) {
  const int raw = kAdcHighThreshold + 100;
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_between_thresholds_holds_previous_high(void) {
  const int raw = (kAdcLowThreshold + kAdcHighThreshold) / 2;
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
}

void test_between_thresholds_holds_previous_low(void) {
  const int raw = (kAdcLowThreshold + kAdcHighThreshold) / 2;
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_high_threshold_boundary_is_high(void) {
  TEST_ASSERT_TRUE(
      cp_adc_level_high(kAdcHighThreshold, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_low_threshold_boundary_is_low(void) {
  TEST_ASSERT_FALSE(
      cp_adc_level_high(kAdcLowThreshold, true, kAdcLowThreshold, kAdcHighThreshold));
}

void test_noise_within_the_band_never_flips_the_level(void) {
  // A few samples wobbling inside the hysteresis band, starting from a known
  // high level. None of them reaches either threshold, so none should flip
  // the level -- this is what stops ADC noise from chattering.
  bool level = true;
  const int wobble[] = {kAdcHighThreshold - 10, kAdcLowThreshold + 10,
                         kAdcHighThreshold - 50, kAdcLowThreshold + 30};
  for (int raw : wobble) {
    level = cp_adc_level_high(raw, level, kAdcLowThreshold, kAdcHighThreshold);
    TEST_ASSERT_TRUE(level);
  }
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_well_below_low_threshold_is_low_regardless_of_previous_level);
  RUN_TEST(test_well_above_high_threshold_is_high_regardless_of_previous_level);
  RUN_TEST(test_between_thresholds_holds_previous_high);
  RUN_TEST(test_between_thresholds_holds_previous_low);
  RUN_TEST(test_high_threshold_boundary_is_high);
  RUN_TEST(test_low_threshold_boundary_is_low);
  RUN_TEST(test_noise_within_the_band_never_flips_the_level);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `~/.platformio/penv/bin/pio test -e native -f test_cp_adc_level`
Expected: build FAILS with `cp_adc_level.h: No such file or directory`.

- [ ] **Step 3: Write the minimal implementation**

Create `lib/cp_adc_level/cp_adc_level.h`:

```cpp
// Decides a digital level from a raw ADC reading, for reading GPIO35's
// Control Pilot signal via the ADC when its HIGH level is too low for the
// ESP32's fixed digital input threshold. Pure logic, no Arduino dependency,
// so it builds and is tested on the host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md.
#pragma once

// Raw ADC counts (12-bit, ADC_11db attenuation, ~0-3.3V over 0-4095 counts).
// Verified and adjustable on the bench against measured adc_raw values.
constexpr int kAdcLowThreshold = 300;   // ~0.24V
constexpr int kAdcHighThreshold = 900;  // ~0.73V

// Hysteresis: goes high once adc_raw >= high_thresh, goes low once
// adc_raw <= low_thresh, otherwise holds previous_level. This is what stops
// ADC noise near a single trip point from chattering.
bool cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh);
```

Create `lib/cp_adc_level/cp_adc_level.cpp`:

```cpp
#include "cp_adc_level.h"

bool cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh) {
  if (adc_raw >= high_thresh) {
    return true;
  }
  if (adc_raw <= low_thresh) {
    return false;
  }
  return previous_level;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native -f test_cp_adc_level`
Expected: `7 Tests 0 Failures 0 Ignored` and `PASSED`.

Then run the full native suite to confirm nothing else broke:

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: `test_cp_convert`, `test_pilot_state` and `test_cp_adc_level` all pass (39 test cases total: 14 + 18 + 7).

- [ ] **Step 5: Commit**

```bash
git add lib/cp_adc_level test/test_cp_adc_level
git commit -m "Add ADC level hysteresis for the low-voltage CP reader

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Rename the ISR reader and add the build environment

**Files:**
- Rename (no content change): `src/cp_reader.cpp` → `src/cp_reader_isr.cpp`
- Modify: `EVSim/platformio.ini`

**Interfaces:**
- Consumes: nothing new.
- Produces: the `[env:esp32dev-lowvoltage]` PlatformIO environment (not yet buildable — `cp_reader_adc.cpp` doesn't exist until Task 3). `[env:esp32dev]` must still build and behave exactly as before.

This task is plumbing, not new behavior, so there is no RED/GREEN test cycle — the check is that the existing build and test suite are unaffected.

- [ ] **Step 1: Rename the file**

```bash
git mv EVSim/src/cp_reader.cpp EVSim/src/cp_reader_isr.cpp
```

Do not edit the file's contents — this is a pure rename.

- [ ] **Step 2: Edit `EVSim/platformio.ini`**

Replace its full contents with:

```ini
; PlatformIO Project Configuration File
;
; EVSim — ESP32 firmware simulating an EV toward a MENNEKES AMTRON charger.
; https://docs.platformio.org/page/projectconf.html

[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino
monitor_speed = 115200
; The ISR-based reader is the default: precise, but needs a clean digital
; signal. See docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md.
build_src_filter = +<*> -<cp_reader_adc.cpp>

[env:esp32dev-lowvoltage]
extends = env:esp32dev
; ADC-based reader for the interim bench setup, where CP's HIGH level falls
; inside the ESP32's undefined digital-input band.
build_src_filter = +<*> -<cp_reader_isr.cpp>

; Host-side tests for the pure code in lib/cp_convert, lib/pilot_state and
; lib/cp_adc_level.
;   pio test -e native
[env:native]
platform = native
test_framework = unity
build_flags = -std=c++14
```

- [ ] **Step 3: Build the firmware to confirm it's unaffected**

Run: `~/.platformio/penv/bin/pio run -e esp32dev`
Expected: `SUCCESS`, same firmware as before (only the source filename changed).

`esp32dev-lowvoltage` is not buildable yet — `cp_reader_adc.cpp` is created in Task 3. Do not attempt to build it here.

- [ ] **Step 4: Re-run the native tests to confirm nothing else broke**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all 39 test cases still pass (this environment doesn't touch `src/`, so this is a safety check, not a new behavior).

- [ ] **Step 5: Commit**

```bash
git add EVSim/platformio.ini
git commit -m "Rename the ISR reader and add the low-voltage build environment

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

`git mv` in Step 1 already staged the rename (both the deletion of the old path and the creation of the new one) — do not `git add` `cp_reader.cpp` or `cp_reader_isr.cpp` again here; the old path no longer exists on disk, and `git add` on it will error. Adding `platformio.ini` alongside the already-staged rename is enough; `git status` before committing should show the rename plus the modified `platformio.ini`, nothing else.

---

### Task 3: ADC sampling task

**Files:**
- Create: `src/cp_reader_adc.cpp`

**Interfaces:**
- Consumes: `kCpPin`, `CpSample` from `src/cp_reader.h` (unchanged); `cp_adc_level_high()`, `kAdcLowThreshold`, `kAdcHighThreshold` from `lib/cp_adc_level/cp_adc_level.h` (Task 1).
- Produces: `void cp_reader_begin()` and `CpSample cp_reader_sample()` — the same signatures `cp_reader_isr.cpp` provides, so `src/main.cpp` needs no changes and works unmodified against either environment.

This file only builds and runs on real hardware (FreeRTOS tasks, the ADC), so there is no native test for it — the check is a successful `esp32dev-lowvoltage` build. On-hardware validation is a separate step, left to the user, per the spec.

- [ ] **Step 1: Write the file**

Create `src/cp_reader_adc.cpp`:

```cpp
// Reads the Control Pilot PWM on GPIO35 via the ADC instead of the digital
// input, for the interim bench setup where CP's HIGH level (~1.45V) falls
// inside the ESP32's undefined digital-input band. See
// docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md.
//
// A FreeRTOS task samples the ADC, timestamps each sample, and decides the
// level via cp_adc_level_high()'s hysteresis, accumulating high-time and
// window time the same way cp_reader_isr.cpp's ISR does -- so
// cp_reader_sample() and CpSample need no changes either way.
#include "cp_reader.h"

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "cp_adc_level.h"

namespace {

// Every this many samples the task rests for one tick so the core's idle
// task still runs -- without it, a tight analogRead() loop can starve the
// idle task and trigger an ESP32 task-watchdog reset.
constexpr int kSamplesPerBurst = 20;

// Written by the sampling task, read by cp_reader_sample() under the same
// lock. Two tasks, not an ISR, so a portMUX_TYPE spinlock is the correct
// primitive here -- noInterrupts()/interrupts() only guard against true ISRs.
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;

uint32_t g_high_us = 0;
uint32_t g_edges = 0;
bool g_last_level_high = false;
int g_last_raw = 0;

uint32_t g_window_start_us = 0;
uint32_t g_last_sample_us = 0;

void cp_adc_task(void *) {
  for (;;) {
    for (int i = 0; i < kSamplesPerBurst; i++) {
      const int raw = analogRead(kCpPin);
      const uint32_t now = micros();

      portENTER_CRITICAL(&g_mux);
      const bool new_level =
          cp_adc_level_high(raw, g_last_level_high, kAdcLowThreshold, kAdcHighThreshold);
      // Attribute the elapsed time since the last sample to whichever level
      // was active over it -- a sample-and-hold integration, not an edge
      // timestamp, since the level is only known at discrete instants.
      if (g_last_level_high) {
        g_high_us += now - g_last_sample_us;  // unsigned wraparound is correct here
      }
      if (new_level != g_last_level_high) {
        g_edges++;
      }
      g_last_level_high = new_level;
      g_last_raw = raw;
      g_last_sample_us = now;
      portEXIT_CRITICAL(&g_mux);
    }
    vTaskDelay(1);
  }
}

}  // namespace

void cp_reader_begin() {
  pinMode(kCpPin, INPUT);
  analogSetPinAttenuation(kCpPin, ADC_11db);  // full 0-3.3V range

  const uint32_t now = micros();
  g_high_us = 0;
  g_edges = 0;
  g_last_level_high = false;
  g_last_raw = analogRead(kCpPin);
  g_last_sample_us = now;
  g_window_start_us = now;

  const BaseType_t created =
      xTaskCreatePinnedToCore(cp_adc_task, "cp_adc", 2048, nullptr, 1, nullptr, 0);
  if (created != pdPASS) {
    // No value is ever invented for an invalid state: leave the accumulators
    // at zero, so cp_reader_sample() reports NO_SIGNAL forever rather than a
    // made-up reading.
    Serial.println("# cp_reader_adc: task creation failed");
  }
}

CpSample cp_reader_sample() {
  const uint32_t now = micros();

  portENTER_CRITICAL(&g_mux);
  const uint32_t high_us = g_high_us;
  const uint32_t edges = g_edges;
  const bool level_high = g_last_level_high;
  const int adc_raw = g_last_raw;
  g_high_us = 0;
  g_edges = 0;
  portEXIT_CRITICAL(&g_mux);

  const uint32_t window_us = now - g_window_start_us;
  g_window_start_us = now;

  CpSample s{};
  s.edges = edges;
  s.level_high = level_high;
  s.window_us = window_us;
  s.adc_raw = adc_raw;

  if (edges == 0 || window_us == 0) {
    s.duty_pct = level_high ? 100.0f : 0.0f;
    s.freq_hz = 0.0f;
    return s;
  }

  s.duty_pct = 100.0f * static_cast<float>(high_us) / static_cast<float>(window_us);
  s.freq_hz = (static_cast<float>(edges) / 2.0f) * 1e6f / static_cast<float>(window_us);
  return s;
}
```

- [ ] **Step 2: Build both firmware environments**

Run: `~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage`
Expected: `SUCCESS`. This is the first successful build of this environment — `cp_reader_adc.cpp` is now the file `build_src_filter` selects for it.

Run: `~/.platformio/penv/bin/pio run -e esp32dev`
Expected: `SUCCESS`, unaffected — this environment still compiles `cp_reader_isr.cpp` and excludes `cp_reader_adc.cpp`.

If the build reports an unresolved symbol for `xTaskCreatePinnedToCore`, `portMUX_TYPE`, `portENTER_CRITICAL`, `vTaskDelay` or `pdPASS`, the `freertos/FreeRTOS.h` / `freertos/task.h` includes above should already cover it — do not guess further; report BLOCKED with the exact compiler error instead of adding unrelated includes speculatively.

- [ ] **Step 3: Re-run the native tests**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all 39 test cases still pass — this file has no host test of its own (see the task's Interfaces note above), so this is a safety check that nothing else regressed.

- [ ] **Step 4: Commit**

```bash
git add src/cp_reader_adc.cpp
git commit -m "Add the ADC-based CP reader for esp32dev-lowvoltage

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Note on-hardware validation (needs the board and the low-voltage bench setup; run by the user)**

Flash with `~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage -t upload`, open the serial monitor at 115200 baud, and confirm:

1. `adc_raw` while CP is high sits near the expected ~1.45 V range (roughly 1700–1900 counts, given the assumed ~0.81 mV/count from `ADC_11db`'s ~0–3.3 V over 0–4095 counts).
2. `duty_pct` roughly tracks the charger's `SignaledCurrentLimit_vehicleif` (via `tools/charger_poll.py`) at a couple of setpoints — see the spec's *Sampling rate and accuracy* section for what precision to expect.
3. Pilot state transitions (`A`/`B`/`C` over serial) still work normally alongside this reader — `pilot_state`/`pilot_driver` are unmodified, but this confirms the two subsystems coexist correctly on real hardware.
