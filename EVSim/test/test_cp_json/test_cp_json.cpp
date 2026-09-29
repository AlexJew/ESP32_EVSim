// Host-side tests for the CP telemetry JSON. Run with:
//   pio test -e native
#include <math.h>
#include <string.h>
#include <unity.h>

#include "cp_json.h"

static CpJsonFields sample() {
  CpJsonFields f;
  f.t_ms = 123456;
  f.freq_hz = 1000.0f;
  f.duty_pct = 24.3f;
  f.adc_raw = 0;
  f.cp = "PWM_VALID";
  f.state = "C";
  f.amps = 14.58f;
  f.watts = 10060.0f;
  f.setpoint_w = 10060;
  f.status = "live";
  f.age_known = true;
  f.age_ms = 0;
  return f;
}

static const char *kExpected =
    "{\"t\":123456,\"freq\":1000.0,\"duty\":24.30,\"adc\":0,\"cp\":\"PWM_VALID\","
    "\"state\":\"C\",\"amps\":14.58,\"watts\":10060,\"setpoint_w\":10060,"
    "\"status\":\"live\",\"age_ms\":0}";

void test_a_full_sample_formats_exactly_as_the_contract(void) {
  char buf[256];
  const int n = cp_json_format(buf, sizeof buf, sample());
  TEST_ASSERT_EQUAL_STRING(kExpected, buf);
  TEST_ASSERT_EQUAL_INT((int)strlen(kExpected), n);
}

void test_missing_current_and_power_are_null(void) {
  CpJsonFields f = sample();
  f.cp = "NO_SIGNAL";
  f.amps = NAN;
  f.watts = NAN;
  f.setpoint_w = 0;
  f.status = "zero";
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"amps\":null,\"watts\":null,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"setpoint_w\":0,\"status\":\"zero\""));
}

void test_an_unknown_age_is_null(void) {
  CpJsonFields f = sample();
  f.age_known = false;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"age_ms\":null}"));
}

void test_non_finite_frequency_and_duty_never_print_a_bare_token(void) {
  CpJsonFields f = sample();
  f.freq_hz = NAN;
  f.duty_pct = INFINITY;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"freq\":null,\"duty\":null,"));
  TEST_ASSERT_NULL(strstr(buf, "nan"));
  TEST_ASSERT_NULL(strstr(buf, "inf"));
}

void test_a_number_too_long_for_its_field_becomes_null_not_a_truncated_number(void) {
  CpJsonFields f = sample();
  f.freq_hz = 1e30f;  // "%.1f" would need 32 characters
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"freq\":null,"));
}

void test_numbers_round_to_their_decimals(void) {
  CpJsonFields f = sample();
  f.duty_pct = 24.999f;
  f.amps = 14.584f;
  f.watts = 10060.4f;
  char buf[256];
  TEST_ASSERT_TRUE(cp_json_format(buf, sizeof buf, f) > 0);
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"duty\":25.00,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"amps\":14.58,"));
  TEST_ASSERT_NOT_NULL(strstr(buf, "\"watts\":10060,"));
}

void test_the_buffer_boundary_is_exact(void) {
  char big[256];
  const int n = cp_json_format(big, sizeof big, sample());
  TEST_ASSERT_TRUE(n > 0);

  char exact[256];
  TEST_ASSERT_EQUAL_INT(n, cp_json_format(exact, (size_t)n + 1, sample()));
  TEST_ASSERT_EQUAL_STRING(big, exact);

  char one_short[256];
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(one_short, (size_t)n, sample()));
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(one_short, 0, sample()));
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(nullptr, 256, sample()));
}

void test_text_that_would_break_the_json_is_rejected(void) {
  char buf[256];
  CpJsonFields f = sample();
  f.cp = "PWM\"VALID";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.state = "C\\";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.status = "li\nve";
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));

  f = sample();
  f.cp = nullptr;
  TEST_ASSERT_EQUAL_INT(-1, cp_json_format(buf, sizeof buf, f));
}

void test_the_worst_case_message_fits_in_256_bytes(void) {
  CpJsonFields f = sample();
  f.t_ms = 4294967295u;
  f.freq_hz = 99999.9f;
  f.duty_pct = 100.0f;
  f.adc_raw = 2147483647;
  f.cp = "OUT_OF_RANGE";
  f.amps = 99.99f;
  f.watts = 99999.0f;
  f.setpoint_w = 2147483647;
  f.status = "held";
  f.age_ms = 4294967295u;
  char buf[256];
  const int n = cp_json_format(buf, sizeof buf, f);
  TEST_ASSERT_TRUE(n > 0);
  TEST_ASSERT_TRUE(n < 256);
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_full_sample_formats_exactly_as_the_contract);
  RUN_TEST(test_missing_current_and_power_are_null);
  RUN_TEST(test_an_unknown_age_is_null);
  RUN_TEST(test_non_finite_frequency_and_duty_never_print_a_bare_token);
  RUN_TEST(test_a_number_too_long_for_its_field_becomes_null_not_a_truncated_number);
  RUN_TEST(test_numbers_round_to_their_decimals);
  RUN_TEST(test_the_buffer_boundary_is_exact);
  RUN_TEST(test_text_that_would_break_the_json_is_rejected);
  RUN_TEST(test_the_worst_case_message_fits_in_256_bytes);
  return UNITY_END();
}
