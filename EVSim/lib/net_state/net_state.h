// WiFi connection policy: when to start an association attempt, when to give
// up on one, and how long to wait before retrying.
//
// Pure logic with no Arduino dependency, so it builds and is tested on the host
// as well as on the ESP32. The caller supplies the clock, which is what makes
// the timeouts testable without waiting real seconds. See
// docs/superpowers/specs/2026-09-25-wifi-static-ip-design.md.
#pragma once

#include <stdint.h>

// Idle: nothing started yet. Connecting: association attempt in flight.
// Connected: the link is up. Backoff: waiting before the next attempt.
enum class NetPhase { Idle, Connecting, Connected, Backoff };

// What the caller must do to reach the returned state. None is the common case.
enum class NetAction { None, BeginConnect, Disconnect };

// An association attempt that has not succeeded within this is a failure.
constexpr uint32_t kNetConnectTimeoutMs = 10000;

// The first retry delay, and the ceiling it doubles up to. The ceiling matters:
// unbounded doubling would eventually stop retrying for hours, while a flat
// short retry would hammer the radio and add avoidable CPU contention to the
// CP ISR.
constexpr uint32_t kNetBackoffMinMs = 1000;
constexpr uint32_t kNetBackoffMaxMs = 30000;

struct NetState {
  NetPhase phase;
  uint32_t phase_started_ms;  // when `phase` was entered
  uint32_t backoff_ms;        // the delay the next Backoff will observe
};

struct NetStep {
  NetState state;    // the resulting state
  NetAction action;  // what the caller must do to reach it
};

// Starting state: Idle, with the backoff wound back to its minimum.
NetState net_state_init(uint32_t now_ms);

// Applies the policy. Takes the current state by value and returns the next one
// together with the action that gets there; the caller owns the single live
// NetState and simply assigns the result back.
//
// `link_up` is the observed link status (on the ESP32, WiFi.status() ==
// WL_CONNECTED). A link that comes up on its own while in Backoff is accepted
// rather than torn down.
NetStep net_step(NetState current, bool link_up, uint32_t now_ms);

// Stable short name for logging: "Idle", "Connecting", "Connected", "Backoff".
const char *net_phase_name(NetPhase phase);
