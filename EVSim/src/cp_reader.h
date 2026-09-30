// Measures the Control Pilot PWM on GPIO35.
//
// An ISR timestamps every edge and accumulates high-time, total-time and an
// edge count. sample() snapshots those counters and resets them, so each call
// reports the window since the previous call rather than an instantaneous
// reading. At 1 kHz a 200 ms window covers ~200 periods, which averages out ISR
// jitter and gives roughly 0.1% duty resolution.
#pragma once

#include <Arduino.h>

// GPIO35 is input-only and sits on ADC1, so it keeps working once WiFi is up.
// The 56k leg of the divider holds it low, so no internal pull is needed --
// which is just as well, since GPIO35 has none.
constexpr uint8_t kCpPin = 35;

struct CpSample {
  float duty_pct;    // 0..100 over the window
  float freq_hz;     // 0 if the line was static, or (ADC reader only) if the window held fewer than two rising edges so no frequency can be measured
  uint32_t edges;    // edges seen in the window
  bool level_high;   // line level at the moment of sampling
  uint32_t window_us;
  int adc_raw;       // GPIO35 through ADC1, to verify the analog level
};

void cp_reader_begin();

// Snapshots and clears the accumulators. Call on a regular cadence.
CpSample cp_reader_sample();
