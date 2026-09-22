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
