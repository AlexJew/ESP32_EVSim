#include "mqtt_proto.h"

MqttRx mqtt_rx_filter(bool retained, int data_len, int total_data_len,
                      int current_offset, bool has_nul) {
  if (retained) {
    return MqttRx::IgnoreRetained;
  }
  if (data_len <= 0 || total_data_len <= 0) {
    return MqttRx::IgnoreEmpty;
  }
  if (current_offset != 0 || data_len != total_data_len ||
      total_data_len >= static_cast<int>(kMqttCommandCapacity) || has_nul) {
    return MqttRx::IgnoreMalformed;
  }
  return MqttRx::Accept;
}

void mqtt_sanitize(const char *src, char *dst, size_t dst_size) {
  if (dst == nullptr || dst_size == 0) {
    return;
  }
  size_t n = 0;
  if (src != nullptr) {
    for (; src[n] != '\0' && n + 1 < dst_size; n++) {
      const unsigned char c = static_cast<unsigned char>(src[n]);
      dst[n] = (c >= 0x20 && c <= 0x7e) ? static_cast<char>(c) : '?';
    }
  }
  dst[n] = '\0';
}
