#include "net_wifi.h"

#include <Arduino.h>
#include <WiFi.h>

#include "net_state.h"
#include "wifi_config.h"

namespace {

NetState g_state;
bool g_begun = false;

}  // namespace

void net_wifi_begin() {
  // Before anything else: stop the SDK persisting credentials to NVS. Every
  // such write is a flash operation, and flash writes disable the instruction
  // cache on BOTH cores -- the one mechanism by which WiFi can genuinely
  // disturb the IRAM_ATTR CP ISR. The credentials are compiled in, so
  // persisting them buys nothing.
  WiFi.persistent(false);

  WiFi.mode(WIFI_STA);  // station only; never bring up the soft AP

  // Modem sleep adds tens of milliseconds of latency to inbound packets, which
  // would later show up as laggy pilot-state commands over MQTT.
  WiFi.setSleep(false);

  // MUST precede WiFi.begin(). Called after, the board associates and then
  // waits out a DHCP lease that will never arrive on this network, which
  // presents as "connects, but has no IP".
  if (!WiFi.config(kStaticIp, kGateway, kSubnetMask, kDns)) {
    Serial.println("# net_wifi: WiFi.config() failed -- static IP NOT applied");
  }

  g_state = net_state_init(millis());
  g_begun = true;

  // Idle -> Connecting on this same pass, so the attempt starts now rather than
  // one loop later. Still non-blocking: it only calls WiFi.begin().
  net_wifi_poll();
}

void net_wifi_poll() {
  if (!g_begun) {
    return;
  }

  const NetPhase was = g_state.phase;
  const NetStep step = net_step(g_state, WiFi.status() == WL_CONNECTED, millis());
  g_state = step.state;

  switch (step.action) {
    case NetAction::BeginConnect:
      WiFi.begin(kWifiSsid, kWifiPassword);
      break;
    case NetAction::Disconnect:
      WiFi.disconnect();
      break;
    case NetAction::None:
      break;
  }

  if (g_state.phase == was) {
    return;
  }

  Serial.print("# net_wifi: ");
  Serial.print(net_phase_name(was));
  Serial.print(" -> ");
  Serial.print(net_phase_name(g_state.phase));
  if (g_state.phase == NetPhase::Connected) {
    Serial.print(", ip ");
    Serial.print(WiFi.localIP());
  } else if (g_state.phase == NetPhase::Backoff) {
    Serial.print(", retry in ");
    Serial.print(g_state.backoff_ms);
    Serial.print(" ms");
  }
  Serial.println();
}

void net_wifi_print_status() {
  // WiFi.status() is printed raw as well as the phase: it is what tells a wrong
  // password (WL_CONNECT_FAILED) apart from an absent AP (WL_NO_SSID_AVAIL),
  // which the policy deliberately treats identically.
  Serial.print("# net_wifi: phase ");
  Serial.print(net_phase_name(g_state.phase));
  Serial.print(", WiFi.status() ");
  Serial.print(static_cast<int>(WiFi.status()));
  Serial.print(", ssid ");
  Serial.println(kWifiSsid);

  Serial.print("# net_wifi: ip ");
  Serial.print(WiFi.localIP());
  Serial.print(", mask ");
  Serial.print(WiFi.subnetMask());
  Serial.print(", gw ");
  Serial.println(WiFi.gatewayIP());

  Serial.print("# net_wifi: rssi ");
  Serial.print(WiFi.RSSI());
  Serial.print(" dBm, channel ");
  Serial.println(WiFi.channel());
}
