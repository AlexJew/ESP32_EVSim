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
    const AdcWindow w = feed_all(pwm(2050, 100, 25, offset));  // not a multiple of the period
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
  // Ends in a HIGH phase (indices 1000..1009 are high), so a take() that reset
  // the level would be caught.
  AdcWindow w = feed_all(pwm(1010, 100, 25));
  const int raw = w.last_raw;
  const AdcWindow taken = adc_window_take(&w);

  TEST_ASSERT_EQUAL_UINT32(1010, taken.samples);
  TEST_ASSERT_TRUE(taken.rises > 0);
  TEST_ASSERT_TRUE(taken.level_high);
  TEST_ASSERT_TRUE(w.level_high);
  TEST_ASSERT_EQUAL_INT(taken.last_raw, w.last_raw);
  TEST_ASSERT_EQUAL_INT(raw, w.last_raw);
  TEST_ASSERT_EQUAL_UINT32(0, w.samples);
  TEST_ASSERT_EQUAL_UINT32(0, w.high);
  TEST_ASSERT_EQUAL_UINT32(0, w.edges);
  TEST_ASSERT_EQUAL_UINT32(0, w.rises);
  TEST_ASSERT_EQUAL_UINT32(0, w.first_rise);
  TEST_ASSERT_EQUAL_UINT32(0, w.last_rise);
  TEST_ASSERT_EQUAL_UINT32(0, w.high_at_first_rise);
  TEST_ASSERT_EQUAL_UINT32(0, w.high_at_last_rise);

  // The high phase continues (positions 10..24), then goes low: no rise, one fall.
  const std::vector<uint16_t> more = pwm(30, 100, 25, 10);
  adc_window_feed(&w, more.data(), more.size(), kAdcLowThreshold, kAdcHighThreshold);
  TEST_ASSERT_EQUAL_UINT32(0, w.rises);
  TEST_ASSERT_EQUAL_UINT32(1, w.edges);
}

void test_a_rising_edge_right_after_a_take_is_seen_at_index_zero(void) {
  AdcWindow w = feed_all(pwm(1050, 100, 25));  // ends in a low phase
  adc_window_take(&w);
  adc_window_feed(&w, &kHigh, 1, kAdcLowThreshold, kAdcHighThreshold);
  TEST_ASSERT_EQUAL_UINT32(1, w.rises);
  TEST_ASSERT_EQUAL_UINT32(0, w.first_rise);
  TEST_ASSERT_EQUAL_UINT32(1, w.edges);
}

void test_swapping_pairs_exchanges_each_adjacent_pair(void) {
  uint16_t v[4] = {1, 2, 3, 4};
  adc_swap_pairs(v, 4);
  TEST_ASSERT_EQUAL_UINT16(2, v[0]);
  TEST_ASSERT_EQUAL_UINT16(1, v[1]);
  TEST_ASSERT_EQUAL_UINT16(4, v[2]);
  TEST_ASSERT_EQUAL_UINT16(3, v[3]);
}

void test_swapping_an_odd_count_leaves_the_trailing_sample(void) {
  uint16_t v[3] = {1, 2, 3};
  adc_swap_pairs(v, 3);
  TEST_ASSERT_EQUAL_UINT16(2, v[0]);
  TEST_ASSERT_EQUAL_UINT16(1, v[1]);
  TEST_ASSERT_EQUAL_UINT16(3, v[2]);
}

void test_swapping_null_or_fewer_than_two_samples_is_harmless(void) {
  adc_swap_pairs(nullptr, 4);
  uint16_t v[2] = {7, 8};
  adc_swap_pairs(v, 0);
  TEST_ASSERT_EQUAL_UINT16(7, v[0]);
  TEST_ASSERT_EQUAL_UINT16(8, v[1]);
  adc_swap_pairs(v, 1);
  TEST_ASSERT_EQUAL_UINT16(7, v[0]);
  TEST_ASSERT_EQUAL_UINT16(8, v[1]);
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
  RUN_TEST(test_a_rising_edge_right_after_a_take_is_seen_at_index_zero);
  RUN_TEST(test_swapping_pairs_exchanges_each_adjacent_pair);
  RUN_TEST(test_swapping_an_odd_count_leaves_the_trailing_sample);
  RUN_TEST(test_swapping_null_or_fewer_than_two_samples_is_harmless);
  RUN_TEST(test_an_empty_window_reports_its_starting_level);
  RUN_TEST(test_feeding_nothing_or_null_is_harmless);
  return UNITY_END();
}
