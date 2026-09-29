// EVSim -- reads the Control Pilot PWM the MENNEKES AMTRON signals on CP and
// reports the current and power it corresponds to, and drives the pilot state
// (A, B or C) the board presents to the charger.
//
// The state is commanded over serial: send A, B or C followed by a newline. It
// can also be commanded over MQTT, by publishing A, B or C on evsim/cmd/state;
// both channels go through the same handler. GPIO32 (state C) is only ever
// asserted while GPIO33 (state B) is, so A -> C is refused. The state is always
// A after reset. Send W over serial for WiFi and MQTT status.
//
// The board also joins the laboratory WLAN on a fixed address (wifi_config.h)
// and keeps an MQTT link to the broker (mqtt_config.h). Neither may block this
// loop: no network condition may delay CP sampling. Publishing the CP
// measurements over MQTT comes later.
//
// Output is CSV sharing column names with tools/charger_poll.py, so the two
// logs can be merged on time to produce the calibration curve. Replies to
// commands are '#'-prefixed lines, so the CSV stays parseable.

#include <Arduino.h>
#include <cmath>
#include <stdio.h>

#include "cp_convert.h"
#include "cp_reader.h"
#include "mqtt_proto.h"
#include "net_mqtt.h"
#include "net_wifi.h"
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

// True for a line holding exactly one 'W' or 'w', ignoring surrounding
// whitespace -- the same leniency pilot_parse_command() gives A/B/C. Kept here
// rather than in lib/pilot_state, because that library is the IEC 61851 state
// model and a network status query is not a pilot state.
bool is_status_command(const char *line) {
  if (line == nullptr) {
    return false;
  }
  const char *p = line;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  if (*p != 'W' && *p != 'w') {
    return false;
  }
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  return *p == '\0';
}

// Where a command came from. A command from MQTT is answered there as well as
// on serial, and cannot ask for the serial-only status report.
enum class Source { Serial, Mqtt };

// Every reply is a '#' line on serial, so the CSV stays parseable; a command
// that arrived over MQTT is answered on evsim/reply too.
void reply(Source source, const char *text) {
  Serial.print("# ");
  Serial.println(text);
  if (source == Source::Mqtt) {
    net_mqtt_publish_reply(text);
  }
}

void handle_command(const char *line, Source source) {
  if (source == Source::Serial && is_status_command(line)) {
    net_wifi_print_status();
    net_mqtt_print_status();
    return;
  }

  char text[64];
  PilotState requested = PilotState::A;
  if (!pilot_parse_command(line, &requested)) {
    // The bytes are untrusted (they may come off the network): show only
    // printable ones, so the reply cannot break the '#' line or the CSV.
    char safe[kLineMax];
    mqtt_sanitize(line, safe, sizeof safe);
    snprintf(text, sizeof text, "unknown command '%s'", safe);
    reply(source, text);
    return;
  }

  if (requested == g_state) {
    snprintf(text, sizeof text, "already in %s", pilot_state_name(g_state));
    reply(source, text);
    return;
  }

  const PilotResult result = pilot_request(g_state, requested);
  if (!result.accepted) {
    snprintf(text, sizeof text, "refused %s -> %s: state B must be asserted first",
             pilot_state_name(g_state), pilot_state_name(requested));
    reply(source, text);
    return;
  }

  pilot_driver_apply(g_state, result.state);
  snprintf(text, sizeof text, "state %s -> %s", pilot_state_name(g_state),
           pilot_state_name(result.state));
  reply(source, text);
  g_state = result.state;
  net_mqtt_publish_state(g_state);
}

// Collects serial bytes into lines and hands each complete line to
// handle_command(). Either CR or LF ends a line, so CRLF terminals work; the
// empty line that CRLF produces is ignored. An overlong line is discarded.
// At most one line is handled per call (the rest stay in the RX buffer for the
// next loop pass), so a burst of commands cannot delay the CP sampling.
void poll_serial() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());

    if (c == '\n' || c == '\r') {
      const bool handled = g_line_overflow || g_line_len > 0;
      if (g_line_overflow) {
        Serial.println("# unknown command (line too long)");
      } else if (g_line_len > 0) {
        g_line[g_line_len] = '\0';
        handle_command(g_line, Source::Serial);
      }
      g_line_len = 0;
      g_line_overflow = false;
      if (handled) {
        return;
      }
      continue;  // empty line (e.g. the LF of CRLF): nothing dispatched
    }

    if (g_line_len < kLineMax - 1) {
      g_line[g_line_len++] = c;
    } else {
      g_line_overflow = true;
    }
  }
}

// Handles at most one queued MQTT command per loop pass, like poll_serial(), so
// a burst of commands cannot delay the CP sampling. Never waits.
void poll_mqtt() {
  char command[kMqttCommandCapacity];
  if (net_mqtt_take_command(command, sizeof command)) {
    handle_command(command, Source::Mqtt);
  }
}

}  // namespace

void setup() {
  // First, so state A is driven by outputs rather than resting on the R8/R9
  // pulldowns during the USB-CDC wait below.
  pilot_driver_begin();

  Serial.begin(115200);
  while (!Serial && millis() < 2000) {
    ;  // brief grace period for USB CDC, but never block a headless boot
  }

  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim -- CP PWM reader on GPIO35, pilot state control on GPIO32/33");
  Serial.println("# send A, B or C to set the pilot state; A -> C is refused");
  Serial.println("# send W for WiFi and MQTT status");

  // After cp_reader_begin(), so CP measurement is live before the radio starts
  // -- the same reasoning that puts pilot_driver_begin() first. Non-blocking:
  // the link will still be down when this returns.
  net_wifi_begin();

  // After net_wifi_begin(): the client only starts once WiFi reports connected.
  // Non-blocking, like everything network-related here.
  net_mqtt_begin();

  Serial.print("# pilot state ");
  Serial.println(pilot_state_name(g_state));
  Serial.println("millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw,commanded");
  g_next_sample_ms = millis() + kSampleIntervalMs;
}

void loop() {
  net_wifi_poll();
  poll_serial();
  poll_mqtt();

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
