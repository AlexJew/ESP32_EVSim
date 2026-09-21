// Drives the two MOSFET gates that switch the pilot resistors onto CP.
//
// Owns GPIO32 and GPIO33 and holds no rules of its own: which writes to make,
// and in what order, comes from pilot_plan() in lib/pilot_state.
#pragma once

#include <Arduino.h>

#include "pilot_state.h"

constexpr uint8_t kPilotPinB = 33;  // Q1, 2k7: state B
constexpr uint8_t kPilotPinC = 32;  // Q2, 1k2 in parallel with 2k7: state C

// Drives both pins low, then makes them outputs, leaving the board in state A.
void pilot_driver_begin();

// Executes the ordered writes that take the board from `from` to `to`.
void pilot_driver_apply(PilotState from, PilotState to);
