// Host-side tests for the CP conversion arithmetic. Run with:
//   pio test -e native
#include <cmath>
#include <unity.h>

#include "cp_convert.h"

static const float kTol = 0.01f;

// --- duty -> current -------------------------------------------------------

void test_linear_range_uses_six_tenths(void) {
  TEST_ASSERT_FLOAT_WITHIN(kTol, 6.0f, cp_duty_to_current(10.0f));
  TEST_ASSERT_FLOAT_WITHIN(kTol, 10.0f, cp_duty_to_current(16.667f));
  TEST_ASSERT_FLOAT_WITHIN(kTol, 16.0f, cp_duty_to_current(26.667f));
  TEST_ASSERT_FLOAT_WITHIN(kTol, 51.0f, cp_duty_to_current(85.0f));
}

void test_upper_range_uses_steeper_slope(void) {
  // I = 2.5 * (duty - 64)
  TEST_ASSERT_FLOAT_WITHIN(kTol, 55.0f, cp_duty_to_current(86.0f));
  TEST_ASSERT_FLOAT_WITHIN(kTol, 80.0f, cp_duty_to_current(96.0f));
}

void test_the_two_formulas_meet_at_the_boundary(void) {
  // 85% is the last point of the linear branch; just above it the steeper
  // branch must not jump backwards.
  float below = cp_duty_to_current(85.0f);
  float above = cp_duty_to_current(85.001f);
  TEST_ASSERT_TRUE(above >= below);
}

void test_duty_below_the_six_amp_floor_offers_no_current(void) {
  TEST_ASSERT_TRUE(std::isnan(cp_duty_to_current(9.9f)));
  TEST_ASSERT_TRUE(std::isnan(cp_duty_to_current(5.0f)));
  TEST_ASSERT_TRUE(std::isnan(cp_duty_to_current(0.0f)));
}

void test_duty_above_the_signalable_range_offers_no_current(void) {
  TEST_ASSERT_TRUE(std::isnan(cp_duty_to_current(96.1f)));
  TEST_ASSERT_TRUE(std::isnan(cp_duty_to_current(100.0f)));
}

// --- current -> power ------------------------------------------------------

void test_power_matches_the_chargers_own_failsafe_value(void) {
  // The AMTRON reports EEBusLPCFailsafePowerUser_eebus = 11040 W at 16 A,
  // which anchors the 3 x 230 V assumption to the real device.
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 11040.0f, cp_current_to_power(16.0f));
}

void test_power_is_linear_in_current(void) {
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 4140.0f, cp_current_to_power(6.0f));
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 6900.0f, cp_current_to_power(10.0f));
}

void test_power_propagates_nan(void) {
  TEST_ASSERT_TRUE(std::isnan(cp_current_to_power(NAN)));
}

// --- classification --------------------------------------------------------

void test_static_high_line_is_dc_twelve_volts(void) {
  // What the charger does between offers when nothing is drawing load.
  TEST_ASSERT_EQUAL_INT((int)CpState::Dc12V,
                        (int)cp_classify(100.0f, 0.0f, true, false));
}

void test_static_low_line_is_no_signal(void) {
  TEST_ASSERT_EQUAL_INT((int)CpState::NoSignal,
                        (int)cp_classify(0.0f, 0.0f, false, false));
}

void test_normal_offer_is_valid(void) {
  TEST_ASSERT_EQUAL_INT((int)CpState::PwmValid,
                        (int)cp_classify(16.667f, 1000.0f, true, true));
}

void test_duty_below_the_floor_is_out_of_range(void) {
  TEST_ASSERT_EQUAL_INT((int)CpState::OutOfRange,
                        (int)cp_classify(5.0f, 1000.0f, true, true));
}

void test_wrong_frequency_is_not_a_pilot_signal(void) {
  TEST_ASSERT_EQUAL_INT((int)CpState::OutOfRange,
                        (int)cp_classify(16.667f, 250.0f, true, true));
  TEST_ASSERT_EQUAL_INT((int)CpState::OutOfRange,
                        (int)cp_classify(16.667f, 5000.0f, true, true));
}

void test_state_names_are_distinct(void) {
  TEST_ASSERT_EQUAL_STRING("NO_SIGNAL", cp_state_name(CpState::NoSignal));
  TEST_ASSERT_EQUAL_STRING("DC_12V", cp_state_name(CpState::Dc12V));
  TEST_ASSERT_EQUAL_STRING("PWM_VALID", cp_state_name(CpState::PwmValid));
  TEST_ASSERT_EQUAL_STRING("OUT_OF_RANGE", cp_state_name(CpState::OutOfRange));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_linear_range_uses_six_tenths);
  RUN_TEST(test_upper_range_uses_steeper_slope);
  RUN_TEST(test_the_two_formulas_meet_at_the_boundary);
  RUN_TEST(test_duty_below_the_six_amp_floor_offers_no_current);
  RUN_TEST(test_duty_above_the_signalable_range_offers_no_current);
  RUN_TEST(test_power_matches_the_chargers_own_failsafe_value);
  RUN_TEST(test_power_is_linear_in_current);
  RUN_TEST(test_power_propagates_nan);
  RUN_TEST(test_static_high_line_is_dc_twelve_volts);
  RUN_TEST(test_static_low_line_is_no_signal);
  RUN_TEST(test_normal_offer_is_valid);
  RUN_TEST(test_duty_below_the_floor_is_out_of_range);
  RUN_TEST(test_wrong_frequency_is_not_a_pilot_signal);
  RUN_TEST(test_state_names_are_distinct);
  return UNITY_END();
}
