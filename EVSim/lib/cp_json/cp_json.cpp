#include "cp_json.h"

#include <cmath>
#include <stdio.h>

namespace {

// `decimals` places, or "null" when the value is not finite or does not fit.
void number_or_null(char *out, size_t size, float value, int decimals) {
  if (!std::isfinite(value)) {
    snprintf(out, size, "null");
    return;
  }
  const int n = snprintf(out, size, "%.*f", decimals, static_cast<double>(value));
  if (n < 0 || static_cast<size_t>(n) >= size) {
    snprintf(out, size, "null");
  }
}

// True for a non-null string of printable ASCII with no quote or backslash.
bool plain_text(const char *s) {
  if (s == nullptr) {
    return false;
  }
  for (; *s != '\0'; s++) {
    const unsigned char c = static_cast<unsigned char>(*s);
    if (c < 0x20 || c > 0x7e || c == '"' || c == '\\') {
      return false;
    }
  }
  return true;
}

}  // namespace

int cp_json_format(char *buf, size_t size, const CpJsonFields &f) {
  if (buf == nullptr || size == 0 || !plain_text(f.cp) || !plain_text(f.state) ||
      !plain_text(f.status)) {
    return -1;
  }

  char freq[16], duty[16], amps[16], watts[16], age[16];
  number_or_null(freq, sizeof freq, f.freq_hz, 1);
  number_or_null(duty, sizeof duty, f.duty_pct, 2);
  number_or_null(amps, sizeof amps, f.amps, 2);
  number_or_null(watts, sizeof watts, f.watts, 0);
  if (f.age_known) {
    snprintf(age, sizeof age, "%lu", static_cast<unsigned long>(f.age_ms));
  } else {
    snprintf(age, sizeof age, "null");
  }

  const int n = snprintf(
      buf, size,
      "{\"t\":%lu,\"freq\":%s,\"duty\":%s,\"adc\":%d,\"cp\":\"%s\","
      "\"state\":\"%s\",\"amps\":%s,\"watts\":%s,\"setpoint_w\":%ld,"
      "\"status\":\"%s\",\"age_ms\":%s}",
      static_cast<unsigned long>(f.t_ms), freq, duty, f.adc_raw, f.cp, f.state,
      amps, watts, static_cast<long>(f.setpoint_w), f.status, age);
  if (n < 0 || static_cast<size_t>(n) >= size) {
    return -1;
  }
  return n;
}
