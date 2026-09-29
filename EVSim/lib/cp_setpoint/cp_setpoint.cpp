#include "cp_setpoint.h"

namespace {

int32_t whole_watts(float watts) {
  return static_cast<int32_t>(watts + 0.5f);  // powers are never negative
}

}  // namespace

SetpointState setpoint_init() {
  return {false, false, 0.0f, 0};
}

SetpointStep setpoint_step(SetpointState prev, bool in_state_c, bool sample_valid,
                           float watts, uint32_t now_ms) {
  if (!in_state_c) {
    return {setpoint_init(), SetpointStatus::Zero, 0, false, 0};
  }

  if (sample_valid) {
    const SetpointState next = {true, false, watts, now_ms};
    return {next, SetpointStatus::Live, whole_watts(watts), true, 0};
  }

  if (!prev.have_valid) {
    return {prev, SetpointStatus::Zero, 0, false, 0};
  }

  const uint32_t age = now_ms - prev.last_valid_ms;
  if (!prev.expired && age <= kSetpointHoldMs) {
    return {prev, SetpointStatus::Held, whole_watts(prev.last_watts), true, age};
  }

  SetpointState expired = prev;
  expired.expired = true;
  return {expired, SetpointStatus::Zero, 0, true, age};
}

const char *setpoint_status_name(SetpointStatus status) {
  switch (status) {
    case SetpointStatus::Live:
      return "live";
    case SetpointStatus::Held:
      return "held";
    case SetpointStatus::Zero:
      return "zero";
  }
  return "zero";
}
