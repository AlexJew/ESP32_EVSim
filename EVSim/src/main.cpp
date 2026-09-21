// EVSim -- reads the Control Pilot PWM the MENNEKES AMTRON signals on CP and
// reports the current and power it corresponds to, and drives the pilot state
// (A, B or C) the board presents to the charger.
//
// The state is commanded over serial: send A, B or C followed by a newline.
// GPIO32 (state C) is only ever asserted while GPIO33 (state B) is, so A -> C
// is refused. The state is always A after reset. Publishing over MQTT, and
// taking commands from it, come later.
//
// Output is CSV sharing column names with tools/charger_poll.py, so the two
// logs can be merged on time to produce the calibration curve. Replies to
// commands are '#'-prefixed lines, so the CSV stays parseable.

#include <Arduino.h>
#include <cmath>

#include "cp_convert.h"
#include "cp_reader.h"
#include "pilot_driver.h"
#include "pilot_state.h"

namespace {

constexpr uint32_t kSampleIntervalMs = 200;
uint32_t g_next_sample_ms = 0;

// The state the board is commanded to present. Always A after reset.
PilotState g_state = PilotState::A;

constexpr size_t kLineMax = 16;
char g_line[kLineMax];
size_t g_line_len = 0;
bool g_line_overflow = false;

void print_float_or_blank(float value, int decimals) {
  if (std::isnan(value)) {
    return;  // empty CSV cell: no value is invented for an invalid state
  }
  Serial.print(value, decimals);
}

void handle_command(const char *line) {
  PilotState requested = PilotState::A;
  if (!pilot_parse_command(line, &requested)) {
    Serial.print("# unknown command '");
    Serial.print(line);
    Serial.println("'");
    return;
  }

  if (requested == g_state) {
    Serial.print("# already in ");
    Serial.println(pilot_state_name(g_state));
    return;
  }

  const PilotResult result = pilot_request(g_state, requested);
  if (!result.accepted) {
    Serial.print("# refused ");
    Serial.print(pilot_state_name(g_state));
    Serial.print(" -> ");
    Serial.print(pilot_state_name(requested));
    Serial.println(": state B must be asserted first");
    return;
  }

  pilot_driver_apply(g_state, result.state);
  Serial.print("# state ");
  Serial.print(pilot_state_name(g_state));
  Serial.print(" -> ");
  Serial.println(pilot_state_name(result.state));
  g_state = result.state;
}

// Collects serial bytes into lines and hands each complete line to
// handle_command(). Either CR or LF ends a line, so CRLF terminals work; the
// empty line that CRLF produces is ignored. An overlong line is discarded.
void poll_serial() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());

    if (c == '\n' || c == '\r') {
      if (g_line_overflow) {
        Serial.println("# unknown command (line too long)");
      } else if (g_line_len > 0) {
        g_line[g_line_len] = '\0';
        handle_command(g_line);
      }
      g_line_len = 0;
      g_line_overflow = false;
      continue;
    }

    if (g_line_len < kLineMax - 1) {
      g_line[g_line_len++] = c;
    } else {
      g_line_overflow = true;
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) {
    ;  // brief grace period for USB CDC, but never block a headless boot
  }

  pilot_driver_begin();
  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim -- CP PWM reader on GPIO35, pilot state control on GPIO32/33");
  Serial.println("# send A, B or C to set the pilot state; A -> C is refused");
  Serial.print("# pilot state ");
  Serial.println(pilot_state_name(g_state));
  Serial.println("millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw,commanded");
  g_next_sample_ms = millis() + kSampleIntervalMs;
}

void loop() {
  poll_serial();

  const uint32_t now = millis();
  if (static_cast<int32_t>(now - g_next_sample_ms) < 0) {
    return;
  }
  g_next_sample_ms = now + kSampleIntervalMs;

  const CpSample sample = cp_reader_sample();
  const CpState state = cp_classify(sample.duty_pct, sample.freq_hz,
                                    sample.level_high, sample.edges > 0);

  float amps = NAN;
  float watts = NAN;
  if (state == CpState::PwmValid) {
    amps = cp_duty_to_current(sample.duty_pct);
    watts = cp_current_to_power(amps);
  }

  Serial.print(now);
  Serial.print(',');
  Serial.print(sample.freq_hz, 1);
  Serial.print(',');
  Serial.print(sample.duty_pct, 2);
  Serial.print(',');
  print_float_or_blank(amps, 2);
  Serial.print(',');
  print_float_or_blank(watts, 0);
  Serial.print(',');
  Serial.print(cp_state_name(state));
  Serial.print(',');
  Serial.print(sample.adc_raw);
  Serial.print(',');
  Serial.println(pilot_state_name(g_state));
}
