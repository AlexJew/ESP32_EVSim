// Host-side tests for the ADC level hysteresis. Run with:
//   pio test -e native
#include <unity.h>

#include "cp_adc_level.h"

// --- hysteresis --------------------------------------------------------

void test_well_below_low_threshold_is_low_regardless_of_previous_level(void) {
  const int raw = kAdcLowThreshold - 100;
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_well_above_high_threshold_is_high_regardless_of_previous_level(void) {
  const int raw = kAdcHighThreshold + 100;
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_between_thresholds_holds_previous_high(void) {
  const int raw = (kAdcLowThreshold + kAdcHighThreshold) / 2;
  TEST_ASSERT_TRUE(cp_adc_level_high(raw, true, kAdcLowThreshold, kAdcHighThreshold));
}

void test_between_thresholds_holds_previous_low(void) {
  const int raw = (kAdcLowThreshold + kAdcHighThreshold) / 2;
  TEST_ASSERT_FALSE(cp_adc_level_high(raw, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_high_threshold_boundary_is_high(void) {
  TEST_ASSERT_TRUE(
      cp_adc_level_high(kAdcHighThreshold, false, kAdcLowThreshold, kAdcHighThreshold));
}

void test_low_threshold_boundary_is_low(void) {
  TEST_ASSERT_FALSE(
      cp_adc_level_high(kAdcLowThreshold, true, kAdcLowThreshold, kAdcHighThreshold));
}

void test_noise_within_the_band_never_flips_the_level(void) {
  // A few samples wobbling inside the hysteresis band, starting from a known
  // high level. None of them reaches either threshold, so none should flip
  // the level -- this is what stops ADC noise from chattering.
  bool level = true;
  const int wobble[] = {kAdcHighThreshold - 10, kAdcLowThreshold + 10,
                         kAdcHighThreshold - 50, kAdcLowThreshold + 30};
  for (int raw : wobble) {
    level = cp_adc_level_high(raw, level, kAdcLowThreshold, kAdcHighThreshold);
    TEST_ASSERT_TRUE(level);
  }
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_well_below_low_threshold_is_low_regardless_of_previous_level);
  RUN_TEST(test_well_above_high_threshold_is_high_regardless_of_previous_level);
  RUN_TEST(test_between_thresholds_holds_previous_high);
  RUN_TEST(test_between_thresholds_holds_previous_low);
  RUN_TEST(test_high_threshold_boundary_is_high);
  RUN_TEST(test_low_threshold_boundary_is_low);
  RUN_TEST(test_noise_within_the_band_never_flips_the_level);
  return UNITY_END();
}
