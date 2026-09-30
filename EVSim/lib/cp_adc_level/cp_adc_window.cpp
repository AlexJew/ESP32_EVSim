#include "cp_adc_window.h"

#include "cp_adc_level.h"

AdcWindow adc_window_init(bool level_high) {
  AdcWindow w = {};
  w.level_high = level_high;
  return w;
}

void adc_swap_pairs(uint16_t *raw, size_t n) {
  if (raw == nullptr) {
    return;
  }
  for (size_t i = 0; i + 1 < n; i += 2) {
    const uint16_t first = raw[i];
    raw[i] = raw[i + 1];
    raw[i + 1] = first;
  }
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
