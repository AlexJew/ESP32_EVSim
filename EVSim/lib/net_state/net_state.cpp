#include "net_state.h"

namespace {

// Unsigned subtraction, so this stays correct when millis() wraps past
// UINT32_MAX between `since_ms` and `now_ms`.
uint32_t elapsed(uint32_t now_ms, uint32_t since_ms) {
  return now_ms - since_ms;
}

// Doubles the delay without ever overflowing or overshooting the ceiling.
uint32_t doubled(uint32_t backoff_ms) {
  if (backoff_ms >= kNetBackoffMaxMs / 2) {
    return kNetBackoffMaxMs;
  }
  return backoff_ms * 2;
}

}  // namespace

NetState net_state_init(uint32_t now_ms) {
  NetState s{};
  s.phase = NetPhase::Idle;
  s.phase_started_ms = now_ms;
  s.backoff_ms = kNetBackoffMinMs;
  return s;
}

NetStep net_step(NetState current, bool link_up, uint32_t now_ms) {
  NetStep out{};
  out.state = current;
  out.action = NetAction::None;

  switch (current.phase) {
    case NetPhase::Idle:
      out.state.phase = NetPhase::Connecting;
      out.state.phase_started_ms = now_ms;
      out.action = NetAction::BeginConnect;
      break;

    case NetPhase::Connecting:
      if (link_up) {
        out.state.phase = NetPhase::Connected;
        out.state.phase_started_ms = now_ms;
        // A working link earns a fresh budget, so a brief AP reboot after days
        // of uptime costs about a second rather than the last grown delay.
        out.state.backoff_ms = kNetBackoffMinMs;
      } else if (elapsed(now_ms, current.phase_started_ms) >= kNetConnectTimeoutMs) {
        out.state.phase = NetPhase::Backoff;
        out.state.phase_started_ms = now_ms;
        // Tear the attempt down rather than leaving the SDK retrying underneath
        // the policy that has just decided to wait.
        out.action = NetAction::Disconnect;
      }
      break;

    case NetPhase::Connected:
      if (!link_up) {
        out.state.phase = NetPhase::Backoff;
        out.state.phase_started_ms = now_ms;
        // No Disconnect: the link is already gone, there is nothing to tear down.
      }
      break;

    case NetPhase::Backoff:
      if (link_up) {
        out.state.phase = NetPhase::Connected;
        out.state.phase_started_ms = now_ms;
        out.state.backoff_ms = kNetBackoffMinMs;
      } else if (elapsed(now_ms, current.phase_started_ms) >= current.backoff_ms) {
        out.state.phase = NetPhase::Connecting;
        out.state.phase_started_ms = now_ms;
        // The delay grows only when it is actually spent, so `backoff_ms` always
        // reads as the wait the current Backoff is observing.
        out.state.backoff_ms = doubled(current.backoff_ms);
        out.action = NetAction::BeginConnect;
      }
      break;
  }

  return out;
}

const char *net_phase_name(NetPhase phase) {
  switch (phase) {
    case NetPhase::Idle:       return "Idle";
    case NetPhase::Connecting: return "Connecting";
    case NetPhase::Connected:  return "Connected";
    case NetPhase::Backoff:    return "Backoff";
  }
  return "?";
}
