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
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "cp_adc_level.h"
#include "cp_adc_window.h"

// Set to 1 (build flag -DCP_ADC_DEBUG=1) to print the sample rate measured against micros() once a second.
#ifndef CP_ADC_DEBUG
#define CP_ADC_DEBUG 0
#endif

namespace {

constexpr int kSampleRateHz = 100000;  // requested from the I2S clock
constexpr int kDmaBufCount = 8;
constexpr int kDmaBufLen = 512;  // samples per DMA buffer (~5 ms)

// The I2S ADC mode delivers 99 217.7 Hz for the 100 kHz request (measured with
// arduino-esp32 2.0.17 / ESP-IDF 4.4): the mean of 325 one-second windows on the
// board. The +/- 1.6 Hz is the one-buffer quantisation of the 325 s total, with no
// buffers dropped in that run. A 1 % error in this constant would move freq_hz by
// about 10 Hz, well inside the classifier's 900-1100 Hz gate, and would not affect
// the duty at all. The value belongs to this framework version: if the framework
// is upgraded, re-measure it (build with -DCP_ADC_DEBUG=1 and average the printed
// rate over several minutes). It comes from the same crystal as the system timer,
// so it does not drift. Single one-second windows read 98 816 or 99 328 samples because the
// DMA delivers whole 512-sample buffers (193 or 194 per second), so only a long
// mean measures the rate. A runtime calibration against the system clock was
// tried and removed: DMA buffers are lost while WiFi starts at boot, which
// biased it low.
constexpr float kSampleRateActualHz = 99217.7f;

// The window is written by the DMA task and taken by cp_reader_sample() under
// one spinlock, held only for one buffer's worth of counting or one copy.
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
AdcWindow g_window;

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
    // The I2S ADC-mode DMA delivers adjacent samples in swapped order; without
    // this, an edge inside a pair made false transitions and the measured
    // frequency read ~1500 Hz instead of 1000 Hz.
    adc_swap_pairs(buf, n);

    portENTER_CRITICAL(&g_mux);
    adc_window_feed(&g_window, buf, n, kAdcLowThreshold, kAdcHighThreshold);
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
  const bool installed = (err == ESP_OK);
  if (err == ESP_OK) err = i2s_set_adc_mode(ADC_UNIT_1, ADC1_CHANNEL_7);  // GPIO35
  // ADC_ATTEN_DB_11 is a deprecated alias of ADC_ATTEN_DB_12 (same value) in this framework.
  if (err == ESP_OK) err = adc1_config_channel_atten(ADC1_CHANNEL_7, ADC_ATTEN_DB_12);
  if (err == ESP_OK) err = i2s_adc_enable(I2S_NUM_0);
  if (err != ESP_OK) {
    // No value is ever invented for an invalid state: the window stays empty, so
    // cp_reader_sample() reports a static line forever rather than a made-up reading.
    Serial.printf("# cp_reader_adc: I2S ADC setup failed (esp_err %d)\n", static_cast<int>(err));
    if (installed) {
      i2s_driver_uninstall(I2S_NUM_0);
    }
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
  portEXIT_CRITICAL(&g_mux);

#if CP_ADC_DEBUG
  // sample() is called every 200 ms: every 5th call, print the rate seen against micros().
  // Single-second values are quantized to whole DMA buffers (193 or 194 per second),
  // so compare the average over a minute or more with the constant, not one line.
  static uint64_t total_samples = 0;
  static uint32_t last_print_us = 0;
  static int calls = 0;
  static bool primed = false;
  total_samples += w.samples;
  if (++calls % 5 == 0) {
    const uint32_t now_us = micros();
    const uint32_t elapsed_us = now_us - last_print_us;
    // The first window holds samples gathered since boot, so its interval is
    // ignored: the first 5th call only records the time and resets the total.
    if (primed && elapsed_us != 0) {
      Serial.printf("# cp_adc: %.1f samples/s (assumed %.1f)\n",
                    static_cast<double>(total_samples) * 1e6 / static_cast<double>(elapsed_us),
                    static_cast<double>(kSampleRateActualHz));
    }
    primed = true;
    last_print_us = now_us;
    total_samples = 0;
  }
#endif

  CpSample s{};
  s.edges = w.edges;
  s.level_high = w.level_high;
  s.adc_raw = w.last_raw;
  s.window_us = static_cast<uint32_t>(static_cast<double>(w.samples) * 1e6 / kSampleRateActualHz);
  s.duty_pct = adc_window_duty_pct(w);
  s.freq_hz = adc_window_freq_hz(w, kSampleRateActualHz);
  return s;
}
