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
