#include "pilot_state.h"

PilotResult pilot_request(PilotState current, PilotState requested) {
  // C is 1k2 in parallel with the 2k7 that B asserts, never a replacement for
  // it, so C is only reachable once B has been commanded.
  if (current == PilotState::A && requested == PilotState::C) {
    return {false, current};
  }
  return {true, requested};
}

PilotPins pilot_pins(PilotState state) {
  switch (state) {
    case PilotState::A:
      return {false, false};
    case PilotState::B:
      return {true, false};
    case PilotState::C:
      return {true, true};
  }
  return {false, false};  // unreachable; state A is the safe default
}

const char *pilot_state_name(PilotState state) {
  switch (state) {
    case PilotState::A:
      return "A";
    case PilotState::B:
      return "B";
    case PilotState::C:
      return "C";
  }
  return "?";
}

int pilot_plan(PilotState from, PilotState to, PilotWrite out[kPilotMaxWrites]) {
  const PilotPins now = pilot_pins(from);
  const PilotPins next = pilot_pins(to);
  const bool going_up = static_cast<int>(to) > static_cast<int>(from);

  int n = 0;
  if (going_up) {
    if (next.b_high != now.b_high) out[n++] = {PilotPin::B, next.b_high};
    if (next.c_high != now.c_high) out[n++] = {PilotPin::C, next.c_high};
  } else {
    if (next.c_high != now.c_high) out[n++] = {PilotPin::C, next.c_high};
    if (next.b_high != now.b_high) out[n++] = {PilotPin::B, next.b_high};
  }
  return n;
}

namespace {

bool is_blank(char c) {
  return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

}  // namespace

bool pilot_parse_command(const char *line, PilotState *out) {
  if (line == nullptr || out == nullptr) {
    return false;
  }

  while (is_blank(*line)) {
    line++;
  }
  const char letter = *line;
  if (letter == '\0') {
    return false;
  }
  line++;

  while (is_blank(*line)) {
    line++;
  }
  if (*line != '\0') {
    return false;  // a second letter or stray characters: not a command
  }

  switch (letter) {
    case 'A':
    case 'a':
      *out = PilotState::A;
      return true;
    case 'B':
    case 'b':
      *out = PilotState::B;
      return true;
    case 'C':
    case 'c':
      *out = PilotState::C;
      return true;
  }
  return false;
}
