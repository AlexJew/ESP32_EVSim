// Pilot state rules: which of the IEC 61851 states A, B, C the board may present
// to the charger, and which GPIO levels produce each one.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the host
// as well as on the ESP32. See
// docs/superpowers/specs/2026-09-21-pilot-state-control-design.md.
#pragma once

// A: no vehicle. B: connected, not ready (2k7 on CP). C: connected, ready (1k2
// in parallel with 2k7, so the B leg stays in circuit).
enum class PilotState { A, B, C };

struct PilotResult {
  bool accepted;    // false only when the request was refused
  PilotState state; // the resulting state; unchanged when refused
};

// Levels for the two MOSFET gates. b_high is GPIO33 (Q1, 2k7); c_high is GPIO32
// (Q2, 1k2).
struct PilotPins {
  bool b_high;
  bool c_high;
};

// Applies the transition table. The only refused request is A -> C, because B
// has not been commanded. Requesting the current state is accepted and leaves
// it unchanged; the caller tells a no-op apart by comparing states.
PilotResult pilot_request(PilotState current, PilotState requested);

// Gate levels that present `state`.
PilotPins pilot_pins(PilotState state);

// Stable short name for logging: "A", "B" or "C".
const char *pilot_state_name(PilotState state);

// One gate to drive. B is GPIO33 (Q1, 2k7); C is GPIO32 (Q2, 1k2).
enum class PilotPin { B, C };

struct PilotWrite {
  PilotPin pin;
  bool high;
};

// The most writes any transition needs.
constexpr int kPilotMaxWrites = 2;

// The ordered writes that take the board from `from` to `to`, touching only the
// pins whose level changes. The order never leaves C high while B is low, even
// briefly: going up sets B then C, going down clears C then B. Returns the
// number of writes placed in `out` (0 when the state does not change).
int pilot_plan(PilotState from, PilotState to, PilotWrite out[kPilotMaxWrites]);
