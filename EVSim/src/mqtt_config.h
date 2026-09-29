// The MQTT broker the board talks to, and the topics it uses.
//
// The broker is addressed by IP, never by hostname: this network has no
// internet route and DNS is unconfirmed (see wifi_config.h). Stage one runs
// Mosquitto on the development computer (192.168.21.137); when a separate
// server takes over, only kMqttBrokerUri changes.
//
// No credentials: stage one is an unauthenticated broker on the laboratory LAN.
// Revisit when the broker leaves the computer.
#pragma once

constexpr char kMqttBrokerUri[] = "mqtt://192.168.21.137:1883";
constexpr char kMqttClientId[] = "evsim";

// Commands in; the state, replies and liveness out. See the spec's topic table.
constexpr char kMqttTopicCmdState[] = "evsim/cmd/state";
constexpr char kMqttTopicState[] = "evsim/state";
constexpr char kMqttTopicReply[] = "evsim/reply";
constexpr char kMqttTopicStatus[] = "evsim/status";

// One JSON message per CP sample: the raw measurement plus the held setpoint.
// QoS 0, not retained; see the CP telemetry spec.
constexpr char kMqttTopicCp[] = "evsim/cp";

// The broker publishes the last-will about 1.5 x this after the board vanishes.
constexpr int kMqttKeepaliveS = 10;

// esp-mqtt's own retry interval toward an unreachable broker.
constexpr int kMqttReconnectMs = 5000;
