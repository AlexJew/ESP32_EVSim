#include "cp_reader.h"

namespace {

// Written by the ISR, read by cp_reader_sample() with interrupts masked.
volatile uint32_t g_high_us = 0;
volatile uint32_t g_edges = 0;
volatile uint32_t g_last_edge_us = 0;
volatile bool g_last_level_high = false;

uint32_t g_window_start_us = 0;

void IRAM_ATTR cp_isr() {
  const uint32_t now = micros();
  const bool level = digitalRead(kCpPin) == HIGH;

  // A rising edge ends a low period; a falling edge ends a high one. Only the
  // high periods are accumulated -- the window length supplies the denominator.
  if (!level && g_last_level_high) {
    g_high_us += now - g_last_edge_us;  // unsigned wraparound is correct here
  }

  g_last_edge_us = now;
  g_last_level_high = level;
  g_edges++;
}

}  // namespace

void cp_reader_begin() {
  pinMode(kCpPin, INPUT);
  analogSetPinAttenuation(kCpPin, ADC_11db);  // full 0-3.3V range

  noInterrupts();
  g_high_us = 0;
  g_edges = 0;
  g_last_edge_us = micros();
  g_last_level_high = digitalRead(kCpPin) == HIGH;
  interrupts();

  g_window_start_us = micros();
  attachInterrupt(digitalPinToInterrupt(kCpPin), cp_isr, CHANGE);
}

CpSample cp_reader_sample() {
  const uint32_t now = micros();

  noInterrupts();
  const uint32_t high_us = g_high_us;
  const uint32_t edges = g_edges;
  const bool level_high = g_last_level_high;
  g_high_us = 0;
  g_edges = 0;
  interrupts();

  const uint32_t window_us = now - g_window_start_us;
  g_window_start_us = now;

  CpSample s{};
  s.edges = edges;
  s.level_high = level_high;
  s.window_us = window_us;
  s.adc_raw = analogRead(kCpPin);

  if (edges == 0 || window_us == 0) {
    // Static line: no duty cycle to report. level_high carries the meaning.
    s.duty_pct = level_high ? 100.0f : 0.0f;
    s.freq_hz = 0.0f;
    return s;
  }

  s.duty_pct = 100.0f * static_cast<float>(high_us) / static_cast<float>(window_us);
  // Two edges per period.
  s.freq_hz = (static_cast<float>(edges) / 2.0f) * 1e6f / static_cast<float>(window_us);
  return s;
}
