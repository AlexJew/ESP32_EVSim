// The held power setpoint: what the board tells its consumer to follow, given
// the stream of CP samples and the pilot state it presents.
//
// The charger's current offer vanishes for a few seconds every 6-12 s when
// nothing draws current, so publishing each sample's power would send 0 W in
// every gap. Instead the last valid value is held through a gap, up to a
// window; it drops to zero the moment the pilot leaves state C, and after the
// window if the charger never comes back. No averaging happens here: every
// Live value is one unfiltered reading, and smoothing is the consumer's job.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the
// host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md.
#pragma once

#include <stdint.h>

// How long the last valid setpoint is held once samples stop being valid.
constexpr uint32_t kSetpointHoldMs = 30000;

enum class SetpointStatus {
  Live,  // state C and this sample is valid
  Held,  // state C, no valid sample now, the last valid one is recent
  Zero,  // nothing to follow: not in state C, nothing valid yet, or expired
};

struct SetpointState {
  bool have_valid;        // a valid sample was seen since entering state C
  bool expired;           // the hold ran out; stays set until state C is left
  float last_watts;       // the last valid power
  uint32_t last_valid_ms; // when it was seen
};

struct SetpointStep {
  SetpointState state;   // carry this into the next call
  SetpointStatus status;
  int32_t setpoint_w;    // whole watts; 0 when Zero
  bool age_known;        // false until a valid sample was seen since entering C
  uint32_t age_ms;       // time since the last valid sample, when age_known
};

// Starting state, and the state after leaving state C: nothing remembered.
SetpointState setpoint_init();

// Advances the rules by one sample. `in_state_c` is whether the board presents
// pilot state C; `sample_valid` is whether this sample is a usable offer, and
// `watts` its power (ignored when not valid). Elapsed time is unsigned
// subtraction, correct across the 49-day millis() wrap; an expired hold stays
// expired so a later wrap cannot revive it.
SetpointStep setpoint_step(SetpointState prev, bool in_state_c, bool sample_valid,
                           float watts, uint32_t now_ms);

// "live", "held" or "zero".
const char *setpoint_status_name(SetpointStatus status);
