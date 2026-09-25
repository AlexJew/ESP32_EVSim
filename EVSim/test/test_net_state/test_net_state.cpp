// Host-side tests for the WiFi connection policy. Run with:
//   pio test -e native
#include <unity.h>

#include "net_state.h"

// Drives the machine to Connected at t=0 and returns the state, so tests about
// what happens after a successful connection do not each repeat the climb.
static NetState connected_state(void) {
  NetState s = net_state_init(0);
  s = net_step(s, false, 0).state;   // Idle -> Connecting
  return net_step(s, true, 5).state; // link came up
}

// --- getting connected -----------------------------------------------------

void test_init_starts_idle_with_the_minimum_backoff(void) {
  const NetState s = net_state_init(1234);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Idle, (int)s.phase);
  TEST_ASSERT_EQUAL_UINT32(1234, s.phase_started_ms);
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);
}

void test_idle_begins_connecting_immediately(void) {
  const NetStep r = net_step(net_state_init(0), false, 0);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)r.action);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)r.state.phase);
}

void test_link_up_while_connecting_reaches_connected_with_no_action(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  const NetStep r = net_step(s, true, 200);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

void test_staying_connected_does_nothing(void) {
  const NetStep r = net_step(connected_state(), true, 60000);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

// --- failing to connect ----------------------------------------------------

void test_connecting_holds_until_the_timeout_then_backs_off(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;

  // One millisecond short of the timeout: still trying, no action.
  const NetStep waiting = net_step(s, false, kNetConnectTimeoutMs - 1);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)waiting.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)waiting.action);

  // At the timeout: give up, and tell the caller to tear the attempt down so
  // the SDK stops retrying on its own before the next begin.
  const NetStep timed_out = net_step(s, false, kNetConnectTimeoutMs);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)timed_out.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::Disconnect, (int)timed_out.action);
}

void test_backoff_issues_no_retry_before_the_delay_elapses(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  s = net_step(s, false, kNetConnectTimeoutMs).state;  // -> Backoff, wait 1000

  const uint32_t entered = s.phase_started_ms;
  const NetStep early = net_step(s, false, entered + kNetBackoffMinMs - 1);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)early.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)early.action);

  const NetStep due = net_step(s, false, entered + kNetBackoffMinMs);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)due.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)due.action);
}

void test_backoff_doubles_and_caps_at_the_maximum(void) {
  // Walk failure after failure and record the delay each Backoff observes.
  const uint32_t expected[] = {1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000};
  const size_t count = sizeof(expected) / sizeof(expected[0]);

  NetState s = net_state_init(0);
  uint32_t now = 0;
  s = net_step(s, false, now).state;  // Idle -> Connecting

  for (size_t i = 0; i < count; i++) {
    now += kNetConnectTimeoutMs;
    s = net_step(s, false, now).state;  // Connecting -> Backoff
    TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)s.phase);

    // The delay in force for this Backoff is what the state carries now.
    TEST_ASSERT_EQUAL_UINT32(expected[i], s.backoff_ms);

    now += expected[i];
    s = net_step(s, false, now).state;  // Backoff -> Connecting
    TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)s.phase);
  }

  // Asserted at the cap, not merely below it.
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMaxMs, s.backoff_ms);
}

void test_a_successful_connection_resets_the_backoff(void) {
  // Fail twice so the backoff has grown past its minimum, then succeed.
  NetState s = net_state_init(0);
  uint32_t now = 0;
  s = net_step(s, false, now).state;
  for (int i = 0; i < 2; i++) {
    now += kNetConnectTimeoutMs;
    s = net_step(s, false, now).state;  // -> Backoff
    now += s.backoff_ms;
    s = net_step(s, false, now).state;  // -> Connecting
  }
  TEST_ASSERT_TRUE(s.backoff_ms > kNetBackoffMinMs);

  now += 100;
  s = net_step(s, true, now).state;  // connected at last
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)s.phase);
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);

  // Prove it by failing again: the wait must be the minimum, not the old value.
  now += 10;
  s = net_step(s, false, now).state;  // link lost -> Backoff
  TEST_ASSERT_EQUAL_UINT32(kNetBackoffMinMs, s.backoff_ms);
}

// --- losing a working link -------------------------------------------------

void test_link_lost_while_connected_backs_off_and_reconnects(void) {
  NetState s = connected_state();

  const NetStep lost = net_step(s, false, 10000);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff, (int)lost.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)lost.action);

  // Because the backoff was reset on the earlier success, the board retries
  // about a second later rather than thirty.
  const NetStep retry = net_step(lost.state, false, 10000 + kNetBackoffMinMs);
  TEST_ASSERT_EQUAL_INT((int)NetAction::BeginConnect, (int)retry.action);
}

void test_a_link_that_returns_during_backoff_is_accepted(void) {
  NetState s = net_step(net_state_init(0), false, 0).state;
  s = net_step(s, false, kNetConnectTimeoutMs).state;  // -> Backoff

  // The SDK reconnected by itself; do not tear it down just to redial.
  const NetStep r = net_step(s, true, s.phase_started_ms + 10);
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connected, (int)r.state.phase);
  TEST_ASSERT_EQUAL_INT((int)NetAction::None, (int)r.action);
}

// --- the clock -------------------------------------------------------------

void test_timeouts_are_correct_across_the_millis_wraparound(void) {
  // Start the attempt 5 s before millis() wraps, so `now` is numerically far
  // smaller than `phase_started_ms` when the timeout is due.
  const uint32_t start = UINT32_MAX - 5000;
  NetState s = net_step(net_state_init(start), false, start).state;
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting, (int)s.phase);

  const uint32_t before = start + (kNetConnectTimeoutMs - 1);  // wraps
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Connecting,
                        (int)net_step(s, false, before).state.phase);

  const uint32_t due = start + kNetConnectTimeoutMs;  // wraps
  TEST_ASSERT_EQUAL_INT((int)NetPhase::Backoff,
                        (int)net_step(s, false, due).state.phase);
}

// --- names -----------------------------------------------------------------

void test_phase_names(void) {
  TEST_ASSERT_EQUAL_STRING("Idle", net_phase_name(NetPhase::Idle));
  TEST_ASSERT_EQUAL_STRING("Connecting", net_phase_name(NetPhase::Connecting));
  TEST_ASSERT_EQUAL_STRING("Connected", net_phase_name(NetPhase::Connected));
  TEST_ASSERT_EQUAL_STRING("Backoff", net_phase_name(NetPhase::Backoff));
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_init_starts_idle_with_the_minimum_backoff);
  RUN_TEST(test_idle_begins_connecting_immediately);
  RUN_TEST(test_link_up_while_connecting_reaches_connected_with_no_action);
  RUN_TEST(test_staying_connected_does_nothing);
  RUN_TEST(test_connecting_holds_until_the_timeout_then_backs_off);
  RUN_TEST(test_backoff_issues_no_retry_before_the_delay_elapses);
  RUN_TEST(test_backoff_doubles_and_caps_at_the_maximum);
  RUN_TEST(test_a_successful_connection_resets_the_backoff);
  RUN_TEST(test_link_lost_while_connected_backs_off_and_reconnects);
  RUN_TEST(test_a_link_that_returns_during_backoff_is_accepted);
  RUN_TEST(test_timeouts_are_correct_across_the_millis_wraparound);
  RUN_TEST(test_phase_names);
  return UNITY_END();
}
