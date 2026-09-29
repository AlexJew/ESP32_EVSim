// Host-side tests for the held-setpoint rules. Run with:
//   pio test -e native
#include <math.h>
#include <string.h>
#include <unity.h>

#include "cp_setpoint.h"

static int st(SetpointStatus s) { return static_cast<int>(s); }

void test_a_valid_sample_in_state_c_is_live(void) {
  const SetpointStep r =
      setpoint_step(setpoint_init(), true, true, 10060.4f, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Live), st(r.status));
  TEST_ASSERT_EQUAL_INT(10060, r.setpoint_w);
  TEST_ASSERT_TRUE(r.age_known);
  TEST_ASSERT_EQUAL_UINT32(0, r.age_ms);
}

void test_the_setpoint_is_rounded_half_up_to_whole_watts(void) {
  const SetpointStep r =
      setpoint_step(setpoint_init(), true, true, 10060.5f, 1000);
  TEST_ASSERT_EQUAL_INT(10061, r.setpoint_w);
}

void test_a_gap_after_a_valid_sample_is_held_with_its_age(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 10000.0f, 1000);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 8400);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(b.status));
  TEST_ASSERT_EQUAL_INT(10000, b.setpoint_w);
  TEST_ASSERT_TRUE(b.age_known);
  TEST_ASSERT_EQUAL_UINT32(7400, b.age_ms);
}

void test_the_hold_lasts_exactly_the_window(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 10000.0f, 1000);
  const SetpointStep at_limit =
      setpoint_step(a.state, true, false, NAN, 1000 + kSetpointHoldMs);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(at_limit.status));
  TEST_ASSERT_EQUAL_INT(10000, at_limit.setpoint_w);

  const SetpointStep past =
      setpoint_step(a.state, true, false, NAN, 1000 + kSetpointHoldMs + 1);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(past.status));
  TEST_ASSERT_EQUAL_INT(0, past.setpoint_w);
  TEST_ASSERT_TRUE(past.age_known);
  TEST_ASSERT_EQUAL_UINT32(kSetpointHoldMs + 1, past.age_ms);
}

void test_state_c_with_no_valid_sample_yet_is_zero_with_unknown_age(void) {
  const SetpointStep r = setpoint_step(setpoint_init(), true, false, NAN, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(r.status));
  TEST_ASSERT_EQUAL_INT(0, r.setpoint_w);
  TEST_ASSERT_FALSE(r.age_known);
}

void test_outside_state_c_is_zero_even_for_a_valid_sample(void) {
  const SetpointStep r = setpoint_step(setpoint_init(), false, true, 8000.0f, 1000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(r.status));
  TEST_ASSERT_EQUAL_INT(0, r.setpoint_w);
  TEST_ASSERT_FALSE(r.age_known);
}

void test_leaving_state_c_forgets_the_held_value(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 8000.0f, 1000);
  // The board is in state B for one sample, then back in C with a gap.
  const SetpointStep b = setpoint_step(a.state, false, false, NAN, 1200);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(b.status));
  TEST_ASSERT_FALSE(b.state.have_valid);
  const SetpointStep c = setpoint_step(b.state, true, false, NAN, 1400);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(c.status));
  TEST_ASSERT_EQUAL_INT(0, c.setpoint_w);
  TEST_ASSERT_FALSE(c.age_known);
}

void test_a_fresh_valid_sample_refreshes_the_hold(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 9000.0f, 1000);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 5000);
  TEST_ASSERT_EQUAL_UINT32(4000, b.age_ms);
  const SetpointStep c = setpoint_step(b.state, true, true, 9500.0f, 6000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Live), st(c.status));
  TEST_ASSERT_EQUAL_INT(9500, c.setpoint_w);
  const SetpointStep d = setpoint_step(c.state, true, false, NAN, 7000);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(d.status));
  TEST_ASSERT_EQUAL_INT(9500, d.setpoint_w);
  TEST_ASSERT_EQUAL_UINT32(1000, d.age_ms);
}

void test_a_hold_that_spans_the_clock_wrap_keeps_the_right_age(void) {
  const uint32_t before_wrap = 0xFFFFFF00u;  // 256 ms before the counter wraps
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 7000.0f, before_wrap);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 500u);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Held), st(b.status));
  TEST_ASSERT_EQUAL_INT(7000, b.setpoint_w);
  TEST_ASSERT_EQUAL_UINT32(756, b.age_ms);  // 256 + 500
}

void test_an_expired_hold_is_not_revived_by_the_clock_wrap(void) {
  const SetpointStep a = setpoint_step(setpoint_init(), true, true, 5000.0f, 100);
  const SetpointStep b = setpoint_step(a.state, true, false, NAN, 30101);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(b.status));
  // The counter later wraps so that (now - last_valid) is again under 30 s.
  const SetpointStep c = setpoint_step(b.state, true, false, NAN, 5100);
  TEST_ASSERT_EQUAL_INT(st(SetpointStatus::Zero), st(c.status));
  TEST_ASSERT_EQUAL_INT(0, c.setpoint_w);
}

void test_status_names(void) {
  TEST_ASSERT_EQUAL_STRING("live", setpoint_status_name(SetpointStatus::Live));
  TEST_ASSERT_EQUAL_STRING("held", setpoint_status_name(SetpointStatus::Held));
  TEST_ASSERT_EQUAL_STRING("zero", setpoint_status_name(SetpointStatus::Zero));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_valid_sample_in_state_c_is_live);
  RUN_TEST(test_the_setpoint_is_rounded_half_up_to_whole_watts);
  RUN_TEST(test_a_gap_after_a_valid_sample_is_held_with_its_age);
  RUN_TEST(test_the_hold_lasts_exactly_the_window);
  RUN_TEST(test_state_c_with_no_valid_sample_yet_is_zero_with_unknown_age);
  RUN_TEST(test_outside_state_c_is_zero_even_for_a_valid_sample);
  RUN_TEST(test_leaving_state_c_forgets_the_held_value);
  RUN_TEST(test_a_fresh_valid_sample_refreshes_the_hold);
  RUN_TEST(test_a_hold_that_spans_the_clock_wrap_keeps_the_right_age);
  RUN_TEST(test_an_expired_hold_is_not_revived_by_the_clock_wrap);
  RUN_TEST(test_status_names);
  return UNITY_END();
}
