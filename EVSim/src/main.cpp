// EVSim -- reads the Control Pilot PWM the MENNEKES AMTRON signals on CP and
// reports the current and power it corresponds to.
//
// Read-only: this firmware does not drive GPIO32/GPIO33, so the pilot state is
// whatever the board's pulldowns leave it at (state A). Driving the states and
// publishing over MQTT come later.
//
// Output is CSV sharing column names with tools/charger_poll.py, so the two
// logs can be merged on time to produce the calibration curve.

#include <Arduino.h>
#include <cmath>

#include "cp_convert.h"
#include "cp_reader.h"

namespace {

constexpr uint32_t kSampleIntervalMs = 200;
uint32_t g_next_sample_ms = 0;

void print_float_or_blank(float value, int decimals) {
  if (std::isnan(value)) {
    return;  // empty CSV cell: no value is invented for an invalid state
  }
  Serial.print(value, decimals);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) {
    ;  // brief grace period for USB CDC, but never block a headless boot
  }

  cp_reader_begin();

  Serial.println();
  Serial.println("# EVSim CP PWM reader -- GPIO35, read-only");
  Serial.println("millis,freq_hz,duty_pct,current_a,power_w,state,adc_raw");
  g_next_sample_ms = millis() + kSampleIntervalMs;
}

void loop() {
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
  Serial.println(sample.adc_raw);
}
