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
  return UNITY_END();
}
