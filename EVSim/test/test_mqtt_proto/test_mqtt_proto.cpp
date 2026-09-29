// Host-side tests for the MQTT message acceptance rules. Run with:
//   pio test -e native
#include <string.h>
#include <unity.h>

#include "mqtt_proto.h"

// --- mqtt_rx_filter --------------------------------------------------------

void test_a_plain_single_chunk_command_is_accepted(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::Accept,
                        (int)mqtt_rx_filter(false, 1, 1, 0, false));
}

void test_a_retained_message_is_ignored(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreRetained,
                        (int)mqtt_rx_filter(true, 1, 1, 0, false));
}

void test_retained_wins_over_every_other_defect(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreRetained,
                        (int)mqtt_rx_filter(true, 0, 0, 0, true));
}

void test_an_empty_payload_is_ignored(void) {
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreEmpty,
                        (int)mqtt_rx_filter(false, 0, 0, 0, false));
}

void test_the_longest_accepted_payload_is_capacity_minus_one(void) {
  const int longest = (int)kMqttCommandCapacity - 1;
  TEST_ASSERT_EQUAL_INT((int)MqttRx::Accept,
                        (int)mqtt_rx_filter(false, longest, longest, 0, false));
  TEST_ASSERT_EQUAL_INT(
      (int)MqttRx::IgnoreMalformed,
      (int)mqtt_rx_filter(false, longest + 1, longest + 1, 0, false));
}

void test_a_fragmented_message_is_ignored(void) {
  // First chunk of a 20-byte message: data_len < total_data_len.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 5, 20, 0, false));
  // A later chunk: non-zero offset.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 5, 20, 5, false));
}

void test_a_payload_containing_a_nul_byte_is_ignored(void) {
  // "A\0x" would otherwise be parsed as the command "A" from its prefix.
  TEST_ASSERT_EQUAL_INT((int)MqttRx::IgnoreMalformed,
                        (int)mqtt_rx_filter(false, 3, 3, 0, true));
}

// --- mqtt_sanitize ---------------------------------------------------------

void test_sanitize_keeps_printable_ascii(void) {
  char out[16];
  mqtt_sanitize("Ab 9~", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("Ab 9~", out);
}

void test_sanitize_replaces_a_trailing_newline(void) {
  char out[16];
  mqtt_sanitize("A\n", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("A?", out);
}

void test_sanitize_replaces_control_and_high_bytes(void) {
  char out[16];
  mqtt_sanitize("\t\x01\x7f\xff\r", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("?????", out);
}

void test_sanitize_truncates_to_the_destination_and_terminates(void) {
  char out[4];
  mqtt_sanitize("ABCDEF", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("ABC", out);
}

void test_sanitize_into_a_one_byte_buffer_yields_the_empty_string(void) {
  char out[1] = {'x'};
  mqtt_sanitize("ABC", out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("", out);
}

void test_sanitize_of_null_yields_the_empty_string(void) {
  char out[4] = {'x', 'x', 'x', 'x'};
  mqtt_sanitize(nullptr, out, sizeof out);
  TEST_ASSERT_EQUAL_STRING("", out);
}

void setUp(void) {}
void tearDown(void) {}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(test_a_plain_single_chunk_command_is_accepted);
  RUN_TEST(test_a_retained_message_is_ignored);
  RUN_TEST(test_retained_wins_over_every_other_defect);
  RUN_TEST(test_an_empty_payload_is_ignored);
  RUN_TEST(test_the_longest_accepted_payload_is_capacity_minus_one);
  RUN_TEST(test_a_fragmented_message_is_ignored);
  RUN_TEST(test_a_payload_containing_a_nul_byte_is_ignored);
  RUN_TEST(test_sanitize_keeps_printable_ascii);
  RUN_TEST(test_sanitize_replaces_a_trailing_newline);
  RUN_TEST(test_sanitize_replaces_control_and_high_bytes);
  RUN_TEST(test_sanitize_truncates_to_the_destination_and_terminates);
  RUN_TEST(test_sanitize_into_a_one_byte_buffer_yields_the_empty_string);
  RUN_TEST(test_sanitize_of_null_yields_the_empty_string);
  return UNITY_END();
}
