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

// The I2S ADC-mode DMA delivers adjacent samples in swapped order (each pair
// arrives as [second, first]). Call this on every buffer before adc_window_feed():
// otherwise an edge inside a pair produces false extra transitions. Swaps
// raw[2k] and raw[2k+1] for every complete pair; a trailing odd sample is left
// alone; a null pointer or n < 2 does nothing.
void adc_swap_pairs(uint16_t *raw, size_t n);

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
