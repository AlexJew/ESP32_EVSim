# Low-Voltage CP Reader — Design

**Date:** 2026-09-22
**Status:** approved
**Scope:** an alternate CP PWM reader, selected at flash time, that reads GPIO35 via the ADC
instead of the digital input, for use while the interim bench setup gives a HIGH level too
low for the ESP32's fixed digital input threshold.

## Purpose

The CP PWM reader (`2026-09-07-cp-pwm-reader-design.md`) measures Control Pilot's duty cycle
with a hardware interrupt on GPIO35's digital input. That relies on the ESP32's fixed input
thresholds (roughly VIL ≤ 0.83 V, VIH ≥ 2.48 V). Without the new PCB's front end, the current
bench setup's CP signal only reaches about **1.45 V** on its HIGH half — squarely inside the
undefined band between those two thresholds, where a digital read is unreliable rather than
simply wrong. The existing interrupt-based reader cannot be trusted against this signal.

GPIO35 is also `ADC1_CH7`, so its analog voltage can be read directly, sidestepping the fixed
digital threshold. This design adds that alternate reader so development and bench testing can
continue without the new PCB. It is explicitly a stopgap: once the new PCB restores a clean
signal, the existing interrupt-based reader is the one to keep using.

## Signal characteristics assumed

Measured on the bench: **HIGH ≈ 1.45 V**, **LOW ≈ 0 V and clean** (no reported ripple), which
is consistent with the existing front end's diode block and pulldown already forcing the
negative half toward 0 V — only the peak level is reduced relative to the ~2.9 V the CP PWM
reader spec designed around. These are the two data points this design's thresholds are
chosen against; see *Error handling* for what happens if that assumption is wrong on a
particular bench setup.

## Non-goals

- **No change to the existing ISR-based reader.** `cp_reader_isr.cpp` (the renamed, unmodified
  original `cp_reader.cpp`), `lib/cp_convert`, `lib/pilot_state`, `src/pilot_driver.*` and
  `src/main.cpp` are untouched. This is purely an alternate implementation of the same
  `cp_reader.h` interface, selected at flash time.
- **No continuous/DMA ADC mode.** The accuracy needed here is coarse — rough current bands
  for development and bench testing, not fine calibration — so simple periodic sampling is
  enough; continuous ADC (up to ~1 MSPS via ESP-IDF's `adc_continuous` API) would match the
  ISR path's ~0.1% resolution but is unnecessary complexity for what's needed now.
- **No automatic threshold calibration.** The ADC thresholds are fixed constants, tuned by
  hand against measured `adc_raw` values, the same way the CP reader spec left the state-C
  margin for bench confirmation rather than deriving it analytically.

## Approach considered and rejected

**One file, one environment, `#ifdef`-selected.** Keep a single `cp_reader.cpp` with both
implementations behind `#ifdef CP_READER_ADC`, toggled by a build flag. Rejected: it mixes two
different hardware strategies in one file behind a macro, and breaks from how this codebase
otherwise keeps one responsibility per file (`cp_reader`, `pilot_driver`, `pilot_state` are
each single-purpose). A separate file and a separate PlatformIO environment, selected the same
way `[env:esp32dev]` and `[env:native]` already are, keeps each reader in its own file with no
macro-gated branching, and switching back once the PCB arrives is just choosing a different
`-e` flag.

## Architecture

### `lib/cp_adc_level/` — pure hysteresis decision, no Arduino dependency

- `bool cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh)`
  — decides the digital level from a raw ADC reading with hysteresis: goes high once
  `adc_raw >= high_thresh`, goes low once `adc_raw <= low_thresh`, otherwise holds
  `previous_level`. The hysteresis band is what stops ADC noise near a single trip point from
  chattering.
- `kAdcHighThreshold = 900` (~0.73 V) and `kAdcLowThreshold = 300` (~0.24 V), assuming the
  existing `ADC_11db` attenuation's ~0–3.3 V over 0–4095 counts. The measured ~1.45 V HIGH
  (~1800 counts) clears the high threshold with a wide margin; the ~0 V LOW sits well under
  the low threshold. Both are named constants, verified and adjustable on the bench against
  measured `adc_raw` values, not buried literals.

### `src/cp_reader_adc.cpp` — ADC sampling glue

Implements the same `src/cp_reader.h` interface as `cp_reader_isr.cpp`
(`cp_reader_begin()`, `cp_reader_sample()`, returning the same `CpSample`), so
`src/main.cpp` needs no changes to use either reader.

- `cp_reader_begin()` starts a FreeRTOS task pinned to core 0. This firmware doesn't use
  WiFi, so core 0 is otherwise idle, and pinning keeps the sampling task off the same core as
  `loop()`, which removes any risk of it delaying the 200 ms CP sample cadence or serial
  polling — that risk doesn't need managing here the way it did for `poll_serial()` in the
  pilot state control work, because this task never runs inline in `loop()` at all.
- The task loops: `analogRead(kCpPin)`, timestamp with `micros()`, decide the new level via
  `cp_adc_level_high()`, and attribute the elapsed time since the last sample to whichever
  level was active — accumulating into the same `high_us` / `edges` / `window_us` shape
  `cp_reader_isr.cpp` already uses, protected by a `portMUX_TYPE` spinlock (the correct
  primitive between two tasks, where `noInterrupts()`/`interrupts()` only guard against true
  ISRs). Every `kSamplesPerBurst = 20` samples it does a `vTaskDelay(1)` so the core's idle
  task still runs — without this, a tight `analogRead()` loop can starve the idle task and
  trigger an ESP32 task-watchdog reset. The task stack is 2048 bytes at priority 1 (the
  default Arduino task priority).
- `cp_reader_sample()` keeps the exact signature and return type as the ISR version,
  snapshotting the same counters under the spinlock. `adc_raw` is the last value the task
  already captured, not a fresh `analogRead()` call from `cp_reader_sample()` itself — the
  sampling task is the sole owner of the ADC on this pin, so no two tasks ever call
  `analogRead(kCpPin)` concurrently.

### `platformio.ini` — selection

A new `[env:esp32dev-lowvoltage]` that `extends = esp32dev` and swaps which `cp_reader_*.cpp`
file is compiled via `build_src_filter`, excluding `cp_reader_isr.cpp` and including
`cp_reader_adc.cpp` (and the reverse for `[env:esp32dev]`). Flash with `-e
esp32dev-lowvoltage` while the signal is weak; flash with the normal `-e esp32dev` once the
new PCB gives a clean signal back. The native-env comment is extended again to cover
`lib/cp_adc_level`.

## Sampling rate and accuracy

The task has no hardware timer driving it — a burst of `kSamplesPerBurst = 20` back-to-back
`analogRead()` calls, then one `vTaskDelay(1)` (≈1 ms, the FreeRTOS tick period) — so the
effective sample rate falls out of how long a burst takes plus that rest, and isn't fixed.
`analogRead()`'s per-call cost varies with the exact arduino-esp32 core version; at roughly
20–150 µs/call this works out to:

| `analogRead()` cost | Burst (20 reads) | Cycle (+ 1 ms rest) | Effective rate | Samples per 1 ms period |
|---|---|---|---|---|
| ~20 µs | 0.4 ms | 1.4 ms | ~14 kHz | ~14 |
| ~150 µs | 3.0 ms | 4.0 ms | ~5 kHz | ~5 |

A transition's true timing is only known to within one sampling interval — roughly 70–200 µs
at these rates, against a 1 ms period. That's a single-edge timing uncertainty on the order of
**±7% to ±20% of one period**. The 200 ms reporting window (~200 periods) should average this
down some, since sampling isn't phase-locked to the charger's own clock, but no specific final
figure is claimed here — this is exactly what the on-hardware validation step below is for.
This is genuinely coarse: likely single-digit-to-teens percent, not fine calibration, though
it should still separate current bands several points apart (6 A ≈ 10% duty, 10 A ≈ 16.7%,
16 A ≈ 26.7%).

**Deferred, not built now:** if this proves too coarse in practice, the cheap next step is
swapping `analogRead()` for ESP-IDF's `adc_oneshot_read()`, which bypasses the Arduino
wrapper's overhead (typically ~10–20 µs/read) for perhaps 2–5× the effective sample rate,
still well short of full continuous/DMA mode. Not needed unless the bench numbers call for it.

## Data flow

```
sampling task (core 0): analogRead() → timestamp → cp_adc_level_high() → accumulate
                                                     high_us / edges / window_us
                                                     (portMUX-protected)

main loop (unchanged):   cp_reader_sample() → same CpSample → cp_classify() /
                                               cp_duty_to_current() → CSV row
```

## Error handling

- **Task creation failure** (`xTaskCreatePinnedToCore` returns `pdFAIL`; expected only under
  memory exhaustion): `cp_reader_begin()` prints `# cp_reader_adc: task creation failed` and
  returns. The accumulators stay at zero, so the reader reports `NO_SIGNAL` forever rather
  than crashing or inventing a value — consistent with the CP PWM reader spec's rule that no
  value is ever invented for an invalid state.
- **Watchdog starvation:** the periodic `vTaskDelay(1)` is load-bearing, not tidiness; without
  it the sampling task can starve its core's idle task and trigger a watchdog reset.
- **Exclusive ADC ownership:** only the sampling task calls `analogRead(kCpPin)`.
  `cp_reader_sample()` never samples again itself.
- **Frequency is not measured, by design:** the ~1 ms burst/rest cadence is comparable to
  CP's own ~1 ms period, so counting edges across a rest gap cannot reliably measure
  frequency — a whole period can be swallowed within a single gap and never register as a
  transition. Duty stays an unbiased estimate over the reporting window (see *Sampling rate
  and accuracy* above), so `cp_reader_adc.cpp` reports the protocol's known nominal
  frequency whenever any transitions are detected, rather than a computed value — which is
  what `cp_classify()`'s 900–1100 Hz gate actually needs (confirmation that a real CP signal
  is present), not a genuine frequency measurement. This is a deliberate limitation of the
  coarse sampling approach, not a bug; `duty_pct` remains this reader's meaningful output.
  Coarse periodic sampling can still miss very narrow pulses (duty near the 3–10% floor).

## Testing

**Native unit tests** (`test/test_cp_adc_level/`, run with `pio test -e native`): the
hysteresis truth table for `cp_adc_level_high()` — well below and well above each threshold,
holding the previous level between the thresholds from both starting sides, the exact
boundary values, and a noise-immunity case where a value oscillating within the hysteresis
band never flips the level.

**On-hardware validation** is the real acceptance test, since the sampling task itself isn't
host-testable. Flash `-e esp32dev-lowvoltage` and run alongside `tools/charger_poll.py`:

1. `adc_raw` while the pilot is in state C and the charger is offering current sits near the
   expected ~1.45 V range (roughly 1700–1900 counts, given the assumed ~0.81 mV/count).
2. `duty_pct` roughly tracks the charger's `SignaledCurrentLimit_vehicleif` at a couple of
   setpoints, accepting coarser accuracy than the ISR path per the accepted limitation above.
3. Pilot state transitions (`A`/`B`/`C` over serial, via the unmodified `pilot_state`/
   `pilot_driver`) still work normally alongside this reader.

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
edge), the frequency from the same edge indices and the constant sample rate of the I2S
clock (99 328 Hz, from the same crystal as the system timer). The I2S ADC-mode DMA delivers
adjacent samples in swapped order, so each buffer's pairs are swapped before counting. On the bench the error fell to 0.37 points (range 22.9–24.1 %). `freq_hz` is
now measured, no longer the nominal 1000 Hz, and a window with a single edge has an
unknown frequency (reported as 0), which the classifier treats as out of range.
