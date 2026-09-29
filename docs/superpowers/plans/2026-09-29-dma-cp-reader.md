# DMA-Based Low-Voltage CP Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the burst-and-rest ADC sampler of the low-voltage CP reader with fixed-rate DMA sampling, and report a truly measured duty *and* frequency, so the values published over MQTT are proper measurements.

**Architecture:** The counting logic is a pure library, `lib/cp_adc_level/cp_adc_window`, unit-tested on the Mac: it turns uniformly spaced ADC samples into high/edge counts and the sample indices of the first and last rising edge. `src/cp_reader_adc.cpp` shrinks to the ESP32 glue: the I2S built-in ADC in DMA mode at ~100 kHz, one task that feeds each DMA buffer into the window, and `cp_reader_sample()` which takes the window and converts counts to duty and frequency. The duty is computed over whole PWM periods only (first to last rising edge), and time comes from the sample count with the sample rate calibrated against the system clock.

**Tech Stack:** PlatformIO, Arduino framework on `esp32dev` (arduino-esp32 2.0.17, ESP-IDF 4.4, compiled as `-std=gnu++11`), `driver/i2s.h` + `driver/adc.h`, Unity on the `native` env (`-std=c++14`). `pio` is not on PATH: use `~/.platformio/penv/bin/pio`. `pio` commands run from `EVSim/`; `git` commands run from the repository root, with repo-relative paths.

**Spec:** `docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md` (Task 2 adds an addendum recording why the design changed). Evidence for the change: on the board, against the charger's own constant REST value (14.0 A = 23.33 % duty), the shipped sampler had an error (RMSE) of 11–13 points (range 12.6–51.6 %); a gapless sampler 1.8 points; a DMA sampler at ~99 kHz 0.37 points (range 22.9–24.1 %). The cause was the burst of 20 reads (100 µs each) followed by a one-tick rest: the sample cycle was locked to the 1 ms FreeRTOS tick, nearly synchronous with the 1 kHz PWM, so the duty estimate depended on a slowly slipping phase.

## Global Constraints

- **Kept exactly:** `CpSample`, `cp_reader_begin()`, `cp_reader_sample()` signatures in `src/cp_reader.h`; the CSV schema; `cp_classify()`/`cp_convert` (not modified); the ISR reader `src/cp_reader_isr.cpp`; the build-env selection in `platformio.ini` (only its `[env:native]` comment changes).
- **Sampling:** I2S built-in ADC, `I2S_NUM_0`, `ADC_UNIT_1`, `ADC1_CHANNEL_7` (GPIO35), attenuation `ADC_ATTEN_DB_11`, requested rate `kSampleRateHz = 100000`, 8 DMA buffers of 512 samples. Samples are 16-bit words; the ADC value is the low 12 bits (`& 0x0FFF`), the upper 4 bits carry the channel number.
- **Level decision:** the existing hysteresis `cp_adc_level_high()` with `kAdcLowThreshold = 300` and `kAdcHighThreshold = 900` (unchanged, in `lib/cp_adc_level/cp_adc_level.h`).
- **Duty** is `100 × high samples between the first and the last rising edge ÷ samples between them` when the window holds at least two rising edges (whole periods only). With fewer: a static window (no edge, or no samples) gives 100 (level high) or 0 (level low); a window with edges but fewer than two rising edges gives `100 × high ÷ samples`.
- **Frequency** is `(rising edges − 1) × sample rate ÷ (last rising-edge index − first rising-edge index)` when the window holds at least two rising edges, else `0`. A window with edges but `freq_hz` 0 therefore classifies as `OUT_OF_RANGE` in the existing `cp_classify()`; a static window keeps `freq_hz` 0 and `edges` 0.
- **Sample rate:** starts at `kInitialRateHz = 99328.0f` (the measured rate for a 100 kHz request) and is replaced by `samples ÷ elapsed time` once at least `kCalibrateAfterUs = 5000000` µs of DMA data have been counted; elapsed time is measured between DMA-chunk arrivals (`esp_timer_get_time()`), never from when `cp_reader_sample()` happens to run.
- **Nothing is invented on failure:** if the I2S/ADC setup fails, print one `#` line and leave the window empty, so the reader reports the static-window values (no edges) forever; a failing `i2s_read()` must not spin (rest one tick before retrying).
- The reader task is pinned to core 0 at priority 2 with a 3072-byte stack; the window is shared with `cp_reader_sample()` under one `portMUX_TYPE` spinlock, held only for the copy or the feed of one DMA buffer.
- Code compiled for the ESP32 must be valid **C++11** (no digit separators, no designated initialisers, no C++14 features); the pure library must also build for `[env:native]` and must not include `Arduino.h` or any ESP-IDF header.
- An optional diagnostic, `CP_ADC_DEBUG` (default 0), prints one `#` line per second with the measured sample rate; it must be printed only from `loop()`'s thread (i.e. from `cp_reader_sample()`).
- Never `git add -A` or `git add .`. `data/` holds untracked captures. Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Most likely first; each line has a test in the task that owns the code.

1. **A window that mixes a static stretch with PWM** (the charger booting, or the signal appearing/disappearing) must report the PWM's own duty, not a value diluted by the static part, and a window with a single edge must have an unknown (0) frequency. Task 1 (transitional and single-edge tests).
2. **The window start phase** must not change the result: the same PWM sampled from a different offset gives the same duty and frequency. Task 1 (phase-offset test).
3. **Noise near the levels** — ADC noise within the high and low plateaus, and dips into the 300–900 hysteresis band — must not create edges. Task 1.
4. **Frequency accuracy from the calibrated sample rate**, including the first seconds after boot before calibration completes. Task 2 (code review of the calibration) and Task 3 (bench: `freq_hz` within 1000 ± 1 Hz from the first sample).
5. **The DMA path must not disturb the WiFi/MQTT links** (spinlock hold time, task priority) and a failing `i2s_read()` or setup must not spin or crash. Task 2 and Task 3 (repeated resets, count of WiFi reconnects).

---

## File Structure

| File | Responsibility |
|---|---|
| `EVSim/lib/cp_adc_level/cp_adc_window.h` / `.cpp` | Create. Pure: accumulate uniformly spaced samples into a window; duty and frequency from it. |
| `EVSim/test/test_cp_adc_window/test_cp_adc_window.cpp` | Create. Host tests for the above. |
| `EVSim/src/cp_reader_adc.cpp` | Rewrite. The I2S/DMA glue and `cp_reader_sample()`. |
| `EVSim/platformio.ini` | Modify. `[env:native]` comment only. |
| `docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md` | Modify. Add an addendum. |

---

### Task 1: `cp_adc_window` — the pure counting logic

**Files:**
- Create: `EVSim/lib/cp_adc_level/cp_adc_window.h`
- Create: `EVSim/lib/cp_adc_level/cp_adc_window.cpp`
- Test: `EVSim/test/test_cp_adc_window/test_cp_adc_window.cpp`

**Interfaces:**
- Consumes: `cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh)` and `kAdcLowThreshold`/`kAdcHighThreshold` from `cp_adc_level.h`.
- Produces (used by Task 2):
  - `struct AdcWindow { uint32_t samples, high, edges, rises, first_rise, last_rise, high_at_first_rise, high_at_last_rise; bool level_high; int last_raw; };`
  - `AdcWindow adc_window_init(bool level_high);`
  - `void adc_window_feed(AdcWindow *w, const uint16_t *raw, size_t n, int low_thresh, int high_thresh);`
  - `AdcWindow adc_window_take(AdcWindow *w);`
  - `float adc_window_duty_pct(const AdcWindow &w);`
  - `float adc_window_freq_hz(const AdcWindow &w, float sample_rate_hz);`

- [ ] **Step 1: Write the failing test**

Create `test/test_cp_adc_window/test_cp_adc_window.cpp`:

```cpp
// Host-side tests for the ADC window counting. Run with:
//   pio test -e native
#include <math.h>
#include <stdint.h>
#include <string.h>
#include <unity.h>

#include <vector>

#include "cp_adc_level.h"
#include "cp_adc_window.h"

static const uint16_t kHigh = 1500;  // the CP high plateau, in ADC counts
static const uint16_t kLow = 100;    // the CP low plateau

// Integer-exact PWM: `period` samples per cycle, the first `high_n` of them high,
// starting `offset` samples into the cycle.
static std::vector<uint16_t> pwm(size_t n, int period, int high_n, int offset = 0) {
  std::vector<uint16_t> v(n);
  for (size_t i = 0; i < n; i++) {
    v[i] = ((static_cast<int>(i) + offset) % period) < high_n ? kHigh : kLow;
  }
  return v;
}

// Starts the window at the level of the first sample, so the window does not
// invent a rising edge at index 0 (in the reader, take() keeps the level between windows).
static AdcWindow feed_all(const std::vector<uint16_t> &v) {
  AdcWindow w = adc_window_init(!v.empty() && v[0] >= kAdcHighThreshold);
  adc_window_feed(&w, v.data(), v.size(), kAdcLowThreshold, kAdcHighThreshold);
  return w;
}

void test_a_static_low_line_has_no_edges_and_zero_duty(void) {
  const AdcWindow w = feed_all(std::vector<uint16_t>(1000, kLow));
  TEST_ASSERT_EQUAL_UINT32(1000, w.samples);
  TEST_ASSERT_EQUAL_UINT32(0, w.edges);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_duty_pct(w));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_freq_hz(w, 100000.0f));
}

void test_a_static_high_line_has_no_edges_and_full_duty(void) {
  const AdcWindow w = feed_all(std::vector<uint16_t>(1000, kHigh));
  TEST_ASSERT_EQUAL_UINT32(0, w.edges);
  TEST_ASSERT_EQUAL_FLOAT(100.0f, adc_window_duty_pct(w));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_freq_hz(w, 100000.0f));
}

void test_an_exact_pwm_gives_the_exact_duty_and_frequency(void) {
  // 20 periods of 100 samples at 100 kHz: 1 kHz, 25 % duty. The window starts
  // high, so the first rising edge is the one at sample 100: 19 rises in all.
  const AdcWindow w = feed_all(pwm(2000, 100, 25));
  TEST_ASSERT_EQUAL_UINT32(19, w.rises);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 25.0f, adc_window_duty_pct(w));
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 1000.0f, adc_window_freq_hz(w, 100000.0f));
}

void test_the_result_does_not_depend_on_where_the_window_starts(void) {
  for (int offset = 0; offset < 100; offset += 13) {
    const AdcWindow w = feed_all(pwm(2000, 100, 25, offset));
    TEST_ASSERT_FLOAT_WITHIN(0.001f, 25.0f, adc_window_duty_pct(w));
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 1000.0f, adc_window_freq_hz(w, 100000.0f));
  }
}

void test_a_sample_rate_that_is_not_a_multiple_of_the_pwm_still_measures_well(void) {
  // 99328 Hz sampling of a 1 kHz, 23.33 % PWM over 200 ms (19866 samples).
  const double fs = 99328.0, duty = 0.2333;
  std::vector<uint16_t> v(19866);
  for (size_t i = 0; i < v.size(); i++) {
    const double phase = fmod(static_cast<double>(i) * 1000.0 / fs, 1.0);
    v[i] = phase < duty ? kHigh : kLow;
  }
  const AdcWindow w = feed_all(v);
  TEST_ASSERT_FLOAT_WITHIN(0.1f, 23.33f, adc_window_duty_pct(w));
  TEST_ASSERT_FLOAT_WITHIN(0.3f, 1000.0f, adc_window_freq_hz(w, static_cast<float>(fs)));
}

void test_noise_inside_the_plateaus_creates_no_edges(void) {
  std::vector<uint16_t> clean = pwm(2000, 100, 25);
  std::vector<uint16_t> noisy = clean;
  for (size_t i = 0; i < noisy.size(); i++) {
    const int jitter = static_cast<int>((i * 7919u) % 201u) - 100;  // -100..+100 counts
    noisy[i] = static_cast<uint16_t>(noisy[i] + jitter);
  }
  const AdcWindow a = feed_all(clean);
  const AdcWindow b = feed_all(noisy);
  TEST_ASSERT_EQUAL_UINT32(a.edges, b.edges);
  TEST_ASSERT_EQUAL_UINT32(a.rises, b.rises);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, adc_window_duty_pct(a), adc_window_duty_pct(b));
}

void test_a_dip_into_the_hysteresis_band_does_not_flip_the_level(void) {
  std::vector<uint16_t> v = pwm(2000, 100, 25);
  // Inside each high phase, dip to 500 counts (between 300 and 900) for 3 samples.
  for (size_t p = 0; p < 20; p++) {
    for (size_t k = 8; k < 11; k++) v[p * 100 + k] = 500;
  }
  const AdcWindow w = feed_all(v);
  TEST_ASSERT_EQUAL_UINT32(19, w.rises);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 25.0f, adc_window_duty_pct(w));
}

void test_a_static_stretch_before_the_pwm_does_not_dilute_the_duty(void) {
  // 3000 samples of steady high, then 50 periods of a 25 % PWM.
  std::vector<uint16_t> v(3000, kHigh);
  const std::vector<uint16_t> p = pwm(5000, 100, 25);
  v.insert(v.end(), p.begin(), p.end());
  const AdcWindow w = feed_all(v);
  // A whole-window fraction would read about 53 %.
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 25.0f, adc_window_duty_pct(w));
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 1000.0f, adc_window_freq_hz(w, 100000.0f));
}

void test_a_single_edge_has_an_unknown_frequency(void) {
  std::vector<uint16_t> v(500, kLow);
  v.insert(v.end(), 500, kHigh);
  const AdcWindow w = feed_all(v);
  TEST_ASSERT_EQUAL_UINT32(1, w.edges);
  TEST_ASSERT_EQUAL_UINT32(1, w.rises);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_freq_hz(w, 100000.0f));
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 50.0f, adc_window_duty_pct(w));  // the whole-window fraction
}

void test_feeding_in_chunks_equals_feeding_at_once(void) {
  const std::vector<uint16_t> v = pwm(3000, 97, 31, 5);
  const AdcWindow whole = feed_all(v);

  AdcWindow chunked = adc_window_init(v[0] >= kAdcHighThreshold);
  for (size_t i = 0; i < v.size(); i += 77) {
    const size_t n = (v.size() - i < 77) ? (v.size() - i) : 77;
    adc_window_feed(&chunked, v.data() + i, n, kAdcLowThreshold, kAdcHighThreshold);
  }
  TEST_ASSERT_EQUAL_UINT32(whole.samples, chunked.samples);
  TEST_ASSERT_EQUAL_UINT32(whole.high, chunked.high);
  TEST_ASSERT_EQUAL_UINT32(whole.edges, chunked.edges);
  TEST_ASSERT_EQUAL_UINT32(whole.rises, chunked.rises);
  TEST_ASSERT_EQUAL_UINT32(whole.first_rise, chunked.first_rise);
  TEST_ASSERT_EQUAL_UINT32(whole.last_rise, chunked.last_rise);
  TEST_ASSERT_EQUAL_UINT32(whole.high_at_first_rise, chunked.high_at_first_rise);
  TEST_ASSERT_EQUAL_UINT32(whole.high_at_last_rise, chunked.high_at_last_rise);
  TEST_ASSERT_EQUAL(whole.level_high, chunked.level_high);
  TEST_ASSERT_EQUAL_INT(whole.last_raw, chunked.last_raw);
}

void test_take_returns_the_counts_and_starts_a_window_at_the_same_level(void) {
  AdcWindow w = feed_all(pwm(1050, 100, 25));  // ends in the middle of a period
  const bool level = w.level_high;
  const int raw = w.last_raw;
  const AdcWindow taken = adc_window_take(&w);

  TEST_ASSERT_EQUAL_UINT32(1050, taken.samples);
  TEST_ASSERT_EQUAL_UINT32(0, w.samples);
  TEST_ASSERT_EQUAL_UINT32(0, w.edges);
  TEST_ASSERT_EQUAL_UINT32(0, w.rises);
  TEST_ASSERT_EQUAL(level, w.level_high);
  TEST_ASSERT_EQUAL_INT(raw, w.last_raw);
}

void test_an_empty_window_reports_its_starting_level(void) {
  TEST_ASSERT_EQUAL_FLOAT(100.0f, adc_window_duty_pct(adc_window_init(true)));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_duty_pct(adc_window_init(false)));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, adc_window_freq_hz(adc_window_init(true), 100000.0f));
}

void test_feeding_nothing_or_null_is_harmless(void) {
  AdcWindow w = adc_window_init(false);
  adc_window_feed(&w, nullptr, 10, kAdcLowThreshold, kAdcHighThreshold);
  adc_window_feed(&w, reinterpret_cast<const uint16_t *>(&w), 0, kAdcLowThreshold, kAdcHighThreshold);
  adc_window_feed(nullptr, nullptr, 0, kAdcLowThreshold, kAdcHighThreshold);
  TEST_ASSERT_EQUAL_UINT32(0, w.samples);
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_static_low_line_has_no_edges_and_zero_duty);
  RUN_TEST(test_a_static_high_line_has_no_edges_and_full_duty);
  RUN_TEST(test_an_exact_pwm_gives_the_exact_duty_and_frequency);
  RUN_TEST(test_the_result_does_not_depend_on_where_the_window_starts);
  RUN_TEST(test_a_sample_rate_that_is_not_a_multiple_of_the_pwm_still_measures_well);
  RUN_TEST(test_noise_inside_the_plateaus_creates_no_edges);
  RUN_TEST(test_a_dip_into_the_hysteresis_band_does_not_flip_the_level);
  RUN_TEST(test_a_static_stretch_before_the_pwm_does_not_dilute_the_duty);
  RUN_TEST(test_a_single_edge_has_an_unknown_frequency);
  RUN_TEST(test_feeding_in_chunks_equals_feeding_at_once);
  RUN_TEST(test_take_returns_the_counts_and_starts_a_window_at_the_same_level);
  RUN_TEST(test_an_empty_window_reports_its_starting_level);
  RUN_TEST(test_feeding_nothing_or_null_is_harmless);
  return UNITY_END();
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `~/.platformio/penv/bin/pio test -e native -f test_cp_adc_window`
Expected: FAIL to build — `cp_adc_window.h: No such file or directory`.

- [ ] **Step 3: Write the header**

Create `lib/cp_adc_level/cp_adc_window.h`:

```cpp
// Turns uniformly spaced ADC samples of the Control Pilot line into the numbers
// the reader reports: duty cycle and frequency.
//
// The samples must come at a fixed rate (the I2S DMA sampler delivers one every
// ~10 us). Time is the sample count, never a clock read, so a delayed task
// cannot distort a measurement. The duty is taken over whole PWM periods only
// (from the first to the last rising edge in the window), so a partial period
// at either end of the window, or a static stretch before or after the PWM,
// does not bias it.
//
// Pure logic with no Arduino or ESP-IDF dependency, so it builds and is tested
// on the host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md.
#pragma once

#include <stddef.h>
#include <stdint.h>

struct AdcWindow {
  uint32_t samples;             // samples fed since the window started
  uint32_t high;                // of those, samples whose level was high
  uint32_t edges;               // level changes, rising and falling
  uint32_t rises;               // rising edges
  uint32_t first_rise;          // index (0-based, in this window) of the first rising edge
  uint32_t last_rise;           // index of the last rising edge
  uint32_t high_at_first_rise;  // `high` just before the sample at first_rise
  uint32_t high_at_last_rise;   // `high` just before the sample at last_rise
  bool level_high;              // the level after the last sample
  int last_raw;                 // the last raw ADC value fed
};

// An empty window that starts at `level_high`.
AdcWindow adc_window_init(bool level_high);

// Feeds `n` raw 12-bit ADC values (0..4095), applying the hysteresis of
// cp_adc_level_high(). A null `w` or `raw` is ignored.
void adc_window_feed(AdcWindow *w, const uint16_t *raw, size_t n, int low_thresh,
                     int high_thresh);

// Returns the window's counts and restarts it empty at the same level (and with
// the same last_raw), so a rising edge across the boundary is still seen.
AdcWindow adc_window_take(AdcWindow *w);

// Duty in percent. With two or more rising edges: the high fraction between the
// first and the last one (whole periods). A window with no edge, or no samples,
// is static and gives 100 (high) or 0 (low). A window with edges but fewer than
// two rising ones gives the whole-window high fraction.
float adc_window_duty_pct(const AdcWindow &w);

// Frequency in Hz: (rising edges - 1) periods over the samples between the first
// and the last rising edge, at `sample_rate_hz`. 0 when there are fewer than two
// rising edges (static, or a single edge: the frequency is unknown).
float adc_window_freq_hz(const AdcWindow &w, float sample_rate_hz);
```

- [ ] **Step 4: Write the implementation**

Create `lib/cp_adc_level/cp_adc_window.cpp`:

```cpp
#include "cp_adc_window.h"

#include "cp_adc_level.h"

AdcWindow adc_window_init(bool level_high) {
  AdcWindow w = {};
  w.level_high = level_high;
  return w;
}

void adc_window_feed(AdcWindow *w, const uint16_t *raw, size_t n, int low_thresh,
                     int high_thresh) {
  if (w == nullptr || raw == nullptr) {
    return;
  }
  for (size_t i = 0; i < n; i++) {
    const int value = raw[i];
    const bool now_high = cp_adc_level_high(value, w->level_high, low_thresh, high_thresh);
    if (now_high != w->level_high) {
      w->edges++;
      if (now_high) {
        if (w->rises == 0) {
          w->first_rise = w->samples;
          w->high_at_first_rise = w->high;
        }
        w->last_rise = w->samples;
        w->high_at_last_rise = w->high;
        w->rises++;
      }
    }
    w->level_high = now_high;
    if (now_high) {
      w->high++;
    }
    w->samples++;
    w->last_raw = value;
  }
}

AdcWindow adc_window_take(AdcWindow *w) {
  const AdcWindow taken = *w;
  const int raw = w->last_raw;
  *w = adc_window_init(w->level_high);
  w->last_raw = raw;
  return taken;
}

float adc_window_duty_pct(const AdcWindow &w) {
  if (w.rises >= 2) {
    return 100.0f * static_cast<float>(w.high_at_last_rise - w.high_at_first_rise) /
           static_cast<float>(w.last_rise - w.first_rise);
  }
  if (w.edges == 0 || w.samples == 0) {
    return w.level_high ? 100.0f : 0.0f;
  }
  return 100.0f * static_cast<float>(w.high) / static_cast<float>(w.samples);
}

float adc_window_freq_hz(const AdcWindow &w, float sample_rate_hz) {
  if (w.rises < 2) {
    return 0.0f;
  }
  return static_cast<float>(static_cast<double>(w.rises - 1) * sample_rate_hz /
                            static_cast<double>(w.last_rise - w.first_rise));
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `~/.platformio/penv/bin/pio test -e native`
Expected: all suites PASS, including `test_cp_adc_window` (13 cases); total 97 test cases succeeded (84 existing + 13). If the non-integer-rate test misses its tolerance, report BLOCKED with the printed values rather than loosening it.

- [ ] **Step 6: Commit**

```bash
git add EVSim/lib/cp_adc_level/cp_adc_window.h EVSim/lib/cp_adc_level/cp_adc_window.cpp EVSim/test/test_cp_adc_window
git commit -m "Add the pure ADC window counting for uniform sampling

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Rewrite the reader as DMA glue, and record why

**Files:**
- Rewrite: `EVSim/src/cp_reader_adc.cpp`
- Modify: `EVSim/platformio.ini` (the `[env:native]` comment only)
- Modify: `docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md` (append an addendum)

**Interfaces:**
- Consumes: `AdcWindow`, `adc_window_init`, `adc_window_feed`, `adc_window_take`, `adc_window_duty_pct`, `adc_window_freq_hz` from Task 1; `cp_adc_level_high`, `kAdcLowThreshold`, `kAdcHighThreshold`; `CpSample`, `kCpPin` from `src/cp_reader.h`.
- Produces: the same `cp_reader_begin()` / `cp_reader_sample()` contract as before, now with a measured `freq_hz`.

- [ ] **Step 1: Replace `src/cp_reader_adc.cpp`**

Overwrite the whole file with:

```cpp
// Reads the Control Pilot PWM on GPIO35 via the ADC instead of the digital
// input, for the interim bench setup where CP's HIGH level (~1.45V) falls
// inside the ESP32's undefined digital-input band. See
// docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md.
//
// The I2S peripheral's built-in ADC mode samples GPIO35 at a fixed rate into DMA
// buffers, with no CPU involvement. One task counts each buffer into a window
// (lib/cp_adc_level/cp_adc_window); cp_reader_sample() takes the window and turns
// its counts into duty and frequency. Time comes from the sample count, so a
// task delayed by the WiFi stack cannot distort a measurement. The earlier
// sampler read the ADC in bursts and rested one FreeRTOS tick between them; that
// cycle was locked to the 1 ms tick, nearly synchronous with the 1 kHz PWM, and
// made the duty wander by several points (see the design spec's addendum).
#include "cp_reader.h"

#include <driver/adc.h>
#include <driver/i2s.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "cp_adc_level.h"
#include "cp_adc_window.h"

// Set to 1 (build flag -DCP_ADC_DEBUG=1) to print the measured sample rate once a second.
#ifndef CP_ADC_DEBUG
#define CP_ADC_DEBUG 0
#endif

namespace {

constexpr int kSampleRateHz = 100000;  // requested from the I2S clock
constexpr int kDmaBufCount = 8;
constexpr int kDmaBufLen = 512;  // samples per DMA buffer (~5 ms)

// What the I2S clock really delivers for that request: its dividers cannot make
// exactly 100 kHz, and the board measured 99 327-99 328 samples/s. Used until
// the rate has been measured against the system clock.
constexpr float kInitialRateHz = 99328.0f;
constexpr int64_t kCalibrateAfterUs = 5000000;

// The window is written by the DMA task and taken by cp_reader_sample() under
// one spinlock, held only for one buffer's worth of counting or one copy.
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
AdcWindow g_window;
int64_t g_first_chunk_us = 0;  // arrival time of the first DMA buffer, 0 until then
int64_t g_last_chunk_us = 0;   // arrival time of the latest one
uint64_t g_counted = 0;        // samples in all buffers after the first

float g_rate_hz = kInitialRateHz;  // only cp_reader_sample() touches it

// Blocks until the DMA has filled a buffer, then counts it.
void cp_adc_dma_task(void *) {
  static uint16_t buf[kDmaBufLen];
  for (;;) {
    size_t got = 0;
    if (i2s_read(I2S_NUM_0, buf, sizeof buf, &got, portMAX_DELAY) != ESP_OK) {
      vTaskDelay(1);  // never spin on a failing driver
      continue;
    }
    const size_t n = got / sizeof(uint16_t);
    for (size_t i = 0; i < n; i++) {
      buf[i] &= 0x0FFF;  // the upper 4 bits carry the channel number
    }
    const int64_t arrived_us = esp_timer_get_time();

    portENTER_CRITICAL(&g_mux);
    adc_window_feed(&g_window, buf, n, kAdcLowThreshold, kAdcHighThreshold);
    if (g_first_chunk_us == 0) {
      g_first_chunk_us = arrived_us;  // its samples predate this instant: count from the next
    } else {
      g_counted += n;
    }
    g_last_chunk_us = arrived_us;
    portEXIT_CRITICAL(&g_mux);
  }
}

}  // namespace

void cp_reader_begin() {
  pinMode(kCpPin, INPUT);
  analogSetPinAttenuation(kCpPin, ADC_11db);  // full 0-3.3V range

  // One conventional read to seed the level; after the I2S ADC starts, the
  // Arduino analogRead() must not be used again.
  const int raw = analogRead(kCpPin);
  g_window = adc_window_init(cp_adc_level_high(raw, /*previous_level=*/false, kAdcLowThreshold,
                                               kAdcHighThreshold));
  g_window.last_raw = raw;

  i2s_config_t cfg = {};
  cfg.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_ADC_BUILT_IN);
  cfg.sample_rate = kSampleRateHz;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_MSB;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = kDmaBufCount;
  cfg.dma_buf_len = kDmaBufLen;
  cfg.use_apll = false;

  esp_err_t err = i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr);
  if (err == ESP_OK) err = i2s_set_adc_mode(ADC_UNIT_1, ADC1_CHANNEL_7);  // GPIO35
  if (err == ESP_OK) err = adc1_config_channel_atten(ADC1_CHANNEL_7, ADC_ATTEN_DB_11);
  if (err == ESP_OK) err = i2s_adc_enable(I2S_NUM_0);
  if (err != ESP_OK) {
    // No value is ever invented for an invalid state: the window stays empty, so
    // cp_reader_sample() reports a static line forever rather than a made-up reading.
    Serial.printf("# cp_reader_adc: I2S ADC setup failed (esp_err %d)\n", static_cast<int>(err));
    return;
  }

  const BaseType_t created =
      xTaskCreatePinnedToCore(cp_adc_dma_task, "cp_adc_dma", 3072, nullptr, 2, nullptr, 0);
  if (created != pdPASS) {
    Serial.println("# cp_reader_adc: task creation failed");
  }
}

CpSample cp_reader_sample() {
  portENTER_CRITICAL(&g_mux);
  const AdcWindow w = adc_window_take(&g_window);
  const uint64_t counted = g_counted;
  const int64_t span_us = g_last_chunk_us - g_first_chunk_us;
  portEXIT_CRITICAL(&g_mux);

  // Measure the real sample rate against the system clock once there is enough
  // data; before that the rate measured on the bench stands in.
  if (span_us >= kCalibrateAfterUs) {
    g_rate_hz = static_cast<float>(static_cast<double>(counted) * 1e6 /
                                   static_cast<double>(span_us));
  }

#if CP_ADC_DEBUG
  static int calls = 0;
  if (++calls % 5 == 0) {  // sample() is called every 200 ms, so once per second
    Serial.printf("# cp_adc: sample rate %.1f Hz (%s)\n", static_cast<double>(g_rate_hz),
                  span_us >= kCalibrateAfterUs ? "measured" : "assumed");
  }
#endif

  CpSample s{};
  s.edges = w.edges;
  s.level_high = w.level_high;
  s.adc_raw = w.last_raw;
  s.window_us = static_cast<uint32_t>(static_cast<double>(w.samples) * 1e6 / g_rate_hz);
  s.duty_pct = adc_window_duty_pct(w);
  s.freq_hz = adc_window_freq_hz(w, g_rate_hz);
  return s;
}
```

- [ ] **Step 2: Update the `[env:native]` comment in `platformio.ini`**

The comment above `[env:native]` lists the libraries the host tests cover. Make sure it names `lib/cp_adc_level` as covering both the level hysteresis and the window counting (for example "lib/cp_adc_level (level hysteresis and window counting)"). If it already lists `lib/cp_adc_level`, only add that parenthetical. Change nothing else in the file.

- [ ] **Step 3: Append the addendum to the design spec**

At the end of `docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md`, append (adapting the heading level to the file's style):

```markdown
## Addendum (2026-09-29): the sampler is DMA-based

The original sampler read the ADC in bursts of 20 and rested one FreeRTOS tick between
bursts, and this document's "Sampling rate and accuracy" section argued the duty stays an
unbiased estimate. On the bench that was wrong in practice. Against the charger's own REST
value (a constant 14.0 A, so 23.33 % duty), the duty read by that sampler had an error
(RMSE) of 11–13 points, ranging 12.6–51.6 %, wandering over several seconds. The mean was
right; individual 200 ms readings were not.

**Cause.** `analogRead()` takes ~100 µs, so a burst lasted ~2 ms and the burst-plus-rest
cycle was locked to the 1 ms tick. That is nearly synchronous with the charger's 1 kHz PWM,
so where each burst landed in the PWM period, and therefore the duty estimate, depended on
the slowly slipping phase between two clocks. A sampler with no rest gap reached 1.8
points; random dither added nothing on top.

**Design now.** The I2S built-in ADC mode samples GPIO35 at a fixed rate (~99.3 kHz) into
DMA buffers, and the duty and frequency are computed from sample counts
(`lib/cp_adc_level/cp_adc_window`): the duty over whole PWM periods (first to last rising
edge), the frequency from the same edge indices and the sample rate calibrated against the
system clock. On the bench the error fell to 0.37 points (range 22.9–24.1 %). `freq_hz` is
now measured, no longer the nominal 1000 Hz, and a window with a single edge has an
unknown frequency (reported as 0), which the classifier treats as out of range.
```

- [ ] **Step 4: Build both environments and run the native tests**

Run: `~/.platformio/penv/bin/pio run -e esp32dev && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage && ~/.platformio/penv/bin/pio test -e native`
Expected: both builds `SUCCESS` with no warnings from `cp_reader_adc.cpp` or `cp_adc_window.cpp` (the default `esp32dev` env builds the ISR reader; check that the build output shows no error); 97 test cases succeeded. If the compile fails on a C++11 restriction or an ESP-IDF name, report BLOCKED with the exact error — do not add compiler flags.

- [ ] **Step 5: Commit**

```bash
git add EVSim/src/cp_reader_adc.cpp EVSim/platformio.ini docs/superpowers/specs/2026-09-22-low-voltage-cp-reader-design.md
git commit -m "Sample the low-voltage CP with I2S DMA and measure duty and frequency

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Bench acceptance (hardware, not for a subagent)

Run by the controller with the human, after Tasks 1–2 are reviewed and committed. Charger on at its **14.0 A** limit (23.33 %, confirmed by its REST value), board on USB, Mosquitto running. Flash: `cd EVSim && ~/.platformio/penv/bin/pio run -e esp32dev-lowvoltage -t upload`.

- [ ] **Step 1: Duty against the ground truth.** Two 60 s captures in state C (`tools/cp_jitter.py capture`, board reset by the RTS script). Expected: mean within 0.5 points of 23.33, spread (sd) at most 0.6, no reading outside 22–25 %.
- [ ] **Step 2: Frequency is really measured.** In the same captures `freq_hz` must read 1000.0 ± 1.0 in every `PWM_VALID` row, including the first rows after boot (before the sample rate is calibrated); confirm it is no longer the constant `1000.0` by checking that some rows differ in the second decimal.
- [ ] **Step 3: Transitions.** With the telemetry stream on (`mosquitto_sub -t evsim/cp`), switch the charger off and on once: while it is off the status is `NO_SIGNAL` (or `DC_12V`), and when it returns the first valid sample must be near 23 % / 14 A, with no single-sample spike (no `watts` above 12000 in the boot sequence).
- [ ] **Step 4: WiFi and MQTT are undisturbed.** Reset the board 10 times (the RTS script); count `Backoff` lines in the serial output of each boot and confirm the MQTT client reconnects each time. Expected: no more than the reconnect rate seen with the previous reader (0–1 in five boots earlier). `W` shows `connected` and `errors 0` after a minute.
- [ ] **Step 5: Debug flag.** Build once with `PLATFORMIO_BUILD_FLAGS="-DCP_ADC_DEBUG=1"` and confirm the `# cp_adc: sample rate ...` line reads about 99 328 Hz, `measured` after 5 s; then reflash without the flag.

---

## Self-Review

**Spec coverage:** DMA sampling at fixed rate → Task 2 code; period-aligned duty and measured frequency → Task 1 (logic and tests) and Task 2 (use); sample-rate calibration → Task 2; static and single-edge windows → Task 1 tests and the constraints; failure paths → Task 2 code (setup failure returns with the window empty; `i2s_read` failure rests a tick); optional debug line → Task 2; documentation of the cause and the fix → Task 2 step 3; acceptance against the charger's true value → Task 3.

**Placeholder scan:** none; every code step has its code, the one edit to `platformio.ini` states its exact intent.

**Type consistency:** `AdcWindow`'s ten members are used identically in the header, the implementation, the tests (`w.rises`, `w.edges`, `w.samples`, `w.level_high`, `w.last_raw`, `w.first_rise`/`last_rise` through the accessors) and `cp_reader_adc.cpp`; `adc_window_take(AdcWindow *)` returns by value everywhere; `adc_window_freq_hz(const AdcWindow &, float)` matches its two call sites.

**Known limits, stated rather than hidden:** the duty carries the threshold bias of the fixed 300/900 counts (about +0.1 points on the bench) and the level plateau must stay clear of the hysteresis band; the calibrated rate improves as the run lengthens (error about 5 ms ÷ elapsed time); the first 5 s use the bench-measured constant, good to about 0.01 %; `cp_classify()` still treats a static window as `DC_12V`/`NO_SIGNAL` by level only.
