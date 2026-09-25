// Joins the laboratory WLAN as a station on the fixed address in wifi_config.h.
//
// Nothing here ever blocks. The policy in lib/net_state decides when to dial
// and when to wait; this file only performs the WiFi calls that policy asks for
// and reports what happened on '#'-prefixed lines, so the CSV stays parseable.
//
// The invariant: no network condition may stop, delay or corrupt CP
// measurement. The board is a bench instrument first and a network node second.
#pragma once

// Configures the interface and starts the first association attempt. Returns
// immediately -- the link will not be up yet.
void net_wifi_begin();

// Advances the connection policy. Call every loop pass; it never waits.
void net_wifi_poll();

// Prints phase, addresses and signal quality as '#' lines, for the 'W' command.
void net_wifi_print_status();
