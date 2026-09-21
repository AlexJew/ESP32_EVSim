#include "pilot_driver.h"

namespace {

uint8_t gpio_for(PilotPin pin) {
  return pin == PilotPin::B ? kPilotPinB : kPilotPinC;
}

}  // namespace

void pilot_driver_begin() {
  // The 100k pulldowns (R8/R9) already hold both gates low through reset. Write
  // the level before switching to output so the pins never glitch high.
  digitalWrite(kPilotPinB, LOW);
  digitalWrite(kPilotPinC, LOW);
  pinMode(kPilotPinB, OUTPUT);
  pinMode(kPilotPinC, OUTPUT);
}

void pilot_driver_apply(PilotState from, PilotState to) {
  PilotWrite writes[kPilotMaxWrites];
  const int n = pilot_plan(from, to, writes);
  for (int i = 0; i < n; i++) {
    digitalWrite(gpio_for(writes[i].pin), writes[i].high ? HIGH : LOW);
  }
}
