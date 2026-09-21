// Host-side tests for the pilot state rules. Run with:
//   pio test -e native
#include <unity.h>

#include "pilot_state.h"

static const PilotState kAll[] = {PilotState::A, PilotState::B, PilotState::C};

// --- transition table ------------------------------------------------------

void test_every_transition_except_a_to_c_is_accepted(void) {
  for (PilotState from : kAll) {
    for (PilotState to : kAll) {
      const PilotResult r = pilot_request(from, to);
      const bool expect_accepted = !(from == PilotState::A && to == PilotState::C);
      TEST_ASSERT_TRUE(r.accepted == expect_accepted);
      if (expect_accepted) {
        TEST_ASSERT_EQUAL_INT((int)to, (int)r.state);
      }
    }
  }
}

void test_a_to_c_is_refused_and_leaves_the_state_unchanged(void) {
  const PilotResult r = pilot_request(PilotState::A, PilotState::C);
  TEST_ASSERT_FALSE(r.accepted);
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)r.state);
  // Pins for the unchanged state are still both low.
  const PilotPins pins = pilot_pins(r.state);
  TEST_ASSERT_FALSE(pins.b_high);
  TEST_ASSERT_FALSE(pins.c_high);
}

void test_requesting_the_current_state_is_accepted_and_unchanged(void) {
  for (PilotState s : kAll) {
    const PilotResult r = pilot_request(s, s);
    TEST_ASSERT_TRUE(r.accepted);
    TEST_ASSERT_EQUAL_INT((int)s, (int)r.state);
  }
}

// --- pin mapping -----------------------------------------------------------

void test_state_a_drives_both_pins_low(void) {
  const PilotPins p = pilot_pins(PilotState::A);
  TEST_ASSERT_FALSE(p.b_high);
  TEST_ASSERT_FALSE(p.c_high);
}

void test_state_b_drives_only_the_b_pin(void) {
  const PilotPins p = pilot_pins(PilotState::B);
  TEST_ASSERT_TRUE(p.b_high);
  TEST_ASSERT_FALSE(p.c_high);
}

void test_state_c_drives_both_pins_high(void) {
  // 1k2 in parallel with 2k7: the B leg stays in circuit.
  const PilotPins p = pilot_pins(PilotState::C);
  TEST_ASSERT_TRUE(p.b_high);
  TEST_ASSERT_TRUE(p.c_high);
}

void test_no_state_asserts_c_without_b(void) {
  for (PilotState s : kAll) {
    const PilotPins p = pilot_pins(s);
    TEST_ASSERT_FALSE(p.c_high && !p.b_high);
  }
}

// --- names -----------------------------------------------------------------

void test_state_names(void) {
  TEST_ASSERT_EQUAL_STRING("A", pilot_state_name(PilotState::A));
  TEST_ASSERT_EQUAL_STRING("B", pilot_state_name(PilotState::B));
  TEST_ASSERT_EQUAL_STRING("C", pilot_state_name(PilotState::C));
}

// --- write plan ------------------------------------------------------------

static void apply_write(PilotPins &pins, const PilotWrite &w) {
  if (w.pin == PilotPin::B) {
    pins.b_high = w.high;
  } else {
    pins.c_high = w.high;
  }
}

void test_plan_never_leaves_c_high_without_b(void) {
  for (PilotState from : kAll) {
    for (PilotState to : kAll) {
      PilotPins pins = pilot_pins(from);
      PilotWrite writes[kPilotMaxWrites];
      const int n = pilot_plan(from, to, writes);
      TEST_ASSERT_TRUE(n >= 0 && n <= kPilotMaxWrites);
      for (int i = 0; i < n; i++) {
        apply_write(pins, writes[i]);
        TEST_ASSERT_FALSE(pins.c_high && !pins.b_high);
      }
      // The plan ends in exactly the pin levels of the target state.
      TEST_ASSERT_TRUE(pins.b_high == pilot_pins(to).b_high);
      TEST_ASSERT_TRUE(pins.c_high == pilot_pins(to).c_high);
    }
  }
}

void test_plan_for_the_same_state_writes_nothing(void) {
  for (PilotState s : kAll) {
    PilotWrite writes[kPilotMaxWrites];
    TEST_ASSERT_EQUAL_INT(0, pilot_plan(s, s, writes));
  }
}

void test_plan_going_up_sets_b_before_c(void) {
  PilotWrite writes[kPilotMaxWrites];
  TEST_ASSERT_EQUAL_INT(2, pilot_plan(PilotState::A, PilotState::C, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[1].pin);
  TEST_ASSERT_TRUE(writes[1].high);
}

void test_plan_going_down_clears_c_before_b(void) {
  PilotWrite writes[kPilotMaxWrites];
  TEST_ASSERT_EQUAL_INT(2, pilot_plan(PilotState::C, PilotState::A, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[1].pin);
  TEST_ASSERT_FALSE(writes[1].high);
}

void test_plan_touches_only_the_pin_that_changes(void) {
  PilotWrite writes[kPilotMaxWrites];

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::A, PilotState::B, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::B, PilotState::C, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_TRUE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::C, PilotState::B, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::C, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);

  TEST_ASSERT_EQUAL_INT(1, pilot_plan(PilotState::B, PilotState::A, writes));
  TEST_ASSERT_EQUAL_INT((int)PilotPin::B, (int)writes[0].pin);
  TEST_ASSERT_FALSE(writes[0].high);
}

// --- command parsing -------------------------------------------------------

void test_parse_accepts_a_b_c_in_either_case(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_TRUE(pilot_parse_command("A", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("b", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("C", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::C, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("a", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::A, (int)s);
}

void test_parse_ignores_surrounding_whitespace_and_line_endings(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_TRUE(pilot_parse_command(" c\r\n", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::C, (int)s);
  TEST_ASSERT_TRUE(pilot_parse_command("\tB  ", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
}

void test_parse_rejects_anything_else(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_FALSE(pilot_parse_command("x", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("1", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("   ", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("AB", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("A B", &s));
  TEST_ASSERT_FALSE(pilot_parse_command("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", &s));
}

void test_parse_leaves_the_output_untouched_on_rejection(void) {
  PilotState s = PilotState::B;
  TEST_ASSERT_FALSE(pilot_parse_command("x", &s));
  TEST_ASSERT_EQUAL_INT((int)PilotState::B, (int)s);
}

void test_parse_rejects_null_pointers(void) {
  PilotState s = PilotState::A;
  TEST_ASSERT_FALSE(pilot_parse_command(nullptr, &s));
  TEST_ASSERT_FALSE(pilot_parse_command("A", nullptr));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_every_transition_except_a_to_c_is_accepted);
  RUN_TEST(test_a_to_c_is_refused_and_leaves_the_state_unchanged);
  RUN_TEST(test_requesting_the_current_state_is_accepted_and_unchanged);
  RUN_TEST(test_state_a_drives_both_pins_low);
  RUN_TEST(test_state_b_drives_only_the_b_pin);
  RUN_TEST(test_state_c_drives_both_pins_high);
  RUN_TEST(test_no_state_asserts_c_without_b);
  RUN_TEST(test_state_names);
  RUN_TEST(test_plan_never_leaves_c_high_without_b);
  RUN_TEST(test_plan_for_the_same_state_writes_nothing);
  RUN_TEST(test_plan_going_up_sets_b_before_c);
  RUN_TEST(test_plan_going_down_clears_c_before_b);
  RUN_TEST(test_plan_touches_only_the_pin_that_changes);
  RUN_TEST(test_parse_accepts_a_b_c_in_either_case);
  RUN_TEST(test_parse_ignores_surrounding_whitespace_and_line_endings);
  RUN_TEST(test_parse_rejects_anything_else);
  RUN_TEST(test_parse_leaves_the_output_untouched_on_rejection);
  RUN_TEST(test_parse_rejects_null_pointers);
  return UNITY_END();
}
