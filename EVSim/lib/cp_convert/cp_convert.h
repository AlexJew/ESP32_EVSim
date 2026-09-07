// Control Pilot duty cycle -> current -> power conversion.
//
// Pure arithmetic with no Arduino dependency, so it builds and is tested on the
// host as well as on the ESP32. Rules follow IEC 61851-1 / evsim.gonium.net; see
// docs/superpowers/specs/2026-09-07-cp-pwm-reader-design.md.
#pragma once

enum class CpState {
  NoSignal,    // steady low: charger off, or no pilot
  Dc12V,       // steady high / >96% duty: no current offered, or DC charging
  PwmValid,    // a usable current offer
  OutOfRange,  // pilot present but outside the signalable range
};

// Nominal grid used for the power setpoint: three phases at 230 V.
// 690 W/A checks out against the charger's own EEBus failsafe value of
// 11040 W at 16 A.
constexpr float kPhaseVoltage = 230.0f;
constexpr int kPhaseCount = 3;
constexpr float kWattsPerAmp = kPhaseVoltage * kPhaseCount;

// Duty boundaries, in percent.
constexpr float kDutyNoSignalBelow = 3.0f;   // below this the pilot is effectively off
constexpr float kDutyMinCurrent = 10.0f;     // 6 A, the lowest current a charger may offer
constexpr float kDutyLinearMax = 85.0f;      // 51 A, where the formula changes slope
constexpr float kDutyMax = 96.0f;            // 80 A, the top of the signalable range

// CP carries 1 kHz; anything well off that is not a pilot signal.
constexpr float kFreqMinHz = 900.0f;
constexpr float kFreqMaxHz = 1100.0f;

// Classifies a measurement window. `has_edges` false means the line was static,
// in which case `level_high` decides between a held +12 V and a held -12 V.
CpState cp_classify(float duty_pct, float freq_hz, bool level_high, bool has_edges);

// Amps offered for a duty cycle, or NaN if the duty carries no current offer.
float cp_duty_to_current(float duty_pct);

// Watts for a current. NaN propagates.
float cp_current_to_power(float amps);

// Stable short name for logging.
const char *cp_state_name(CpState state);
