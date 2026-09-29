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
  if (err == ESP_OK) err = adc1_config_channel_atten(ADC1_CHANNEL_7, ADC_ATTEN_DB_12);  // 11 dB renamed 12 dB in IDF 5; same range
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
