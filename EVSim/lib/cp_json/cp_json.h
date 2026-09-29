// Formats one CP sample, with its held setpoint, as the JSON message published
// on evsim/cp.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the
// host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md for the field
// contract.
#pragma once

#include <stddef.h>
#include <stdint.h>

struct CpJsonFields {
  uint32_t t_ms;        // board uptime, millis()
  float freq_hz;
  float duty_pct;
  int adc_raw;
  const char *cp;       // "NO_SIGNAL", "DC_12V", "PWM_VALID" or "OUT_OF_RANGE"
  const char *state;    // pilot state "A", "B" or "C"
  float amps;           // NaN -> null
  float watts;          // NaN -> null
  int32_t setpoint_w;
  const char *status;   // "live", "held" or "zero"
  bool age_known;       // false -> null
  uint32_t age_ms;
};

// Writes the message into `buf` and returns its length, excluding the NUL.
// Returns -1 -- writing nothing usable -- for a null or empty buffer, a buffer
// too small for the whole message (never a truncated message), or a string
// field that is null or holds a quote, a backslash or a byte outside printable
// ASCII, none of which the fixed names ever contain. A non-finite number, or
// one too long for its field, is written as null rather than as a bare
// nan/inf token or a silently shortened number.
int cp_json_format(char *buf, size_t size, const CpJsonFields &fields);
