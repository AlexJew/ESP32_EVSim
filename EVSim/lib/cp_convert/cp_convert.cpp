#include "cp_convert.h"

#include <cmath>

CpState cp_classify(float duty_pct, float freq_hz, bool level_high, bool has_edges) {
  // A static line carries no duty cycle. Which level it rests at is the whole
  // signal: the charger parks CP at +12 V between offers when nothing draws
  // load, and that is normal rather than a fault.
  if (!has_edges) {
    return level_high ? CpState::Dc12V : CpState::NoSignal;
  }

  if (freq_hz < kFreqMinHz || freq_hz > kFreqMaxHz) {
    return CpState::OutOfRange;
  }

  if (duty_pct > kDutyMax) {
    return CpState::Dc12V;
  }
  if (duty_pct < kDutyNoSignalBelow) {
    return CpState::NoSignal;
  }
  if (duty_pct < kDutyMinCurrent) {
    return CpState::OutOfRange;
  }
  return CpState::PwmValid;
}

float cp_duty_to_current(float duty_pct) {
  if (duty_pct < kDutyMinCurrent || duty_pct > kDutyMax) {
    return NAN;
  }
  if (duty_pct <= kDutyLinearMax) {
    return 0.6f * duty_pct;  // 6 A to 51 A
  }
  return 2.5f * (duty_pct - 64.0f);  // 51 A to 80 A
}

float cp_current_to_power(float amps) {
  return amps * kWattsPerAmp;
}

const char *cp_state_name(CpState state) {
  switch (state) {
    case CpState::NoSignal:
      return "NO_SIGNAL";
    case CpState::Dc12V:
      return "DC_12V";
    case CpState::PwmValid:
      return "PWM_VALID";
    case CpState::OutOfRange:
      return "OUT_OF_RANGE";
  }
  return "UNKNOWN";
}
