// The laboratory WLAN the board joins, and the address it takes there.
//
// The network runs NO DHCP server, so the address is fixed here rather than
// leased. All three devices in the thesis loop -- the AMTRON charger
// (192.168.21.117), this board, and the Fronius inverter (192.168.21.122) --
// share 192.168.21.0/24, so none of that traffic depends on the gateway.
//
// There is no internet route on this network: no NTP, hence no wall-clock time,
// and DNS is unconfirmed. Address every peer by IP, never by hostname.
//
// NOTE: these credentials are committed deliberately, which writes them into
// this repository's history permanently. Everything secret lives in this one
// file and nothing else reads it, so moving to a gitignored copy plus a
// committed wifi_config.example.h is a .gitignore line and a file copy, with no
// code change anywhere. Do that before making this repository public.
#pragma once

#include <IPAddress.h>

constexpr char kWifiSsid[] = "hems";
constexpr char kWifiPassword[] = "HEMS1CoSES";

const IPAddress kStaticIp(192, 168, 21, 138);
const IPAddress kSubnetMask(255, 255, 255, 0);
const IPAddress kGateway(192, 168, 21, 130);

// Assumed, not confirmed: the WLAN gateway is the likely DNS relay. Nothing in
// the firmware resolves a hostname, so a dead value here costs nothing.
const IPAddress kDns(192, 168, 21, 130);
