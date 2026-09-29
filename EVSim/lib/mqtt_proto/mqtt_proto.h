// Rules for an incoming MQTT message that might become a pilot-state command,
// and for echoing untrusted bytes safely.
//
// Pure logic with no Arduino or FreeRTOS dependency, so it builds and is tested
// on the host as well as on the ESP32. See
// docs/superpowers/specs/2026-09-29-mqtt-state-commands-design.md.
#pragma once

#include <stddef.h>

// Longest command text the board accepts, terminator included. Equal to the
// serial line buffer in main.cpp, so both channels take the same input.
constexpr size_t kMqttCommandCapacity = 16;

enum class MqttRx {
  Accept,           // may become a command
  IgnoreRetained,   // a retained message is a stored event, not a live command
  IgnoreEmpty,      // no payload
  IgnoreMalformed,  // fragmented, too long, or contains a NUL byte
};

// Decides whether a message received on the command topic may become a
// command. The arguments are the fields of esp-mqtt's DATA event.
//
// A command is an event, not a setting: a retained message would be replayed
// at every reconnect and change the state the charger sees, so it is never
// accepted. Anything that is not a single, complete, NUL-free chunk shorter
// than kMqttCommandCapacity is refused rather than truncated, because a
// truncated prefix could parse as a valid command the publisher never sent.
// `retained` is checked first.
MqttRx mqtt_rx_filter(bool retained, int data_len, int total_data_len,
                      int current_offset, bool has_nul);

// Copies `src` into `dst`, replacing every byte outside printable ASCII
// (0x20..0x7E) with '?', so untrusted input can be echoed on a '#' line without
// breaking the line or the CSV. Always terminates `dst`; truncates to fit. A
// null `src` or a zero-size `dst` is handled (the latter is left untouched).
void mqtt_sanitize(const char *src, char *dst, size_t dst_size);
