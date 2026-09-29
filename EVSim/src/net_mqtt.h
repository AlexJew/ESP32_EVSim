// MQTT command and status link, over the WiFi connection net_wifi maintains.
//
// The invariant, stronger than net_wifi's: loop() NEVER calls into the esp-mqtt
// client. Its publish/enqueue/start/stop calls can block for seconds (network
// timeout, or the client lock while its task connects to a dead broker), and no
// network condition may delay CP sampling. Everything that touches the client
// runs in esp-mqtt's own task or in one small pump task; loop() talks to them
// only through FreeRTOS queues -- the control queue and a one-slot telemetry
// mailbox -- never waiting. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md and
// docs/superpowers/specs/2026-09-29-mqtt-cp-telemetry-design.md.
#pragma once

#include <stddef.h>

#include "pilot_state.h"

// Creates the queues, the client and the pump task. Returns immediately; the
// link is not up yet. Call after net_wifi_begin(). If any allocation fails,
// prints one '#' line and leaves MQTT disabled -- the rest of the firmware is
// unaffected.
void net_mqtt_begin();

// Copies the oldest queued command into `out` (NUL-terminated) and returns
// true, or returns false at once when none is queued. `capacity` must be at
// least kMqttCommandCapacity. Call at most once per loop pass.
bool net_mqtt_take_command(char *out, size_t capacity);

// Records `state` as the state to announce on (re)connect and queues it for
// publishing on evsim/state. Never blocks; a full queue drops the item.
void net_mqtt_publish_state(PilotState state);

// Queues one line for evsim/reply. Never blocks; a full queue drops the line.
void net_mqtt_publish_reply(const char *text);

// Longest telemetry message, terminator included.
constexpr size_t kMqttCpCapacity = 256;

// Hands one CP telemetry message (a JSON text) to be published on evsim/cp at
// QoS 0. It goes into a one-slot mailbox: an unsent older message is replaced,
// so the newest sample always wins and a slow network never queues stale data.
// Never blocks. A message of kMqttCpCapacity characters or more is refused
// rather than truncated. Nothing is sent while the client is disconnected.
void net_mqtt_publish_cp(const char *json);

// Prints broker, connection flags and drop counters as '#' lines, for the 'W'
// command.
void net_mqtt_print_status();
