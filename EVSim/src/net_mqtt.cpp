#include "net_mqtt.h"

#include <Arduino.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <mqtt_client.h>
#include <string.h>

#include <atomic>

#include "mqtt_config.h"
#include "mqtt_proto.h"

namespace {

struct RxItem {
  char text[kMqttCommandCapacity];
};

enum class TxKind : uint8_t { State, Reply };

struct TxItem {
  TxKind kind;
  char text[64];
};

constexpr UBaseType_t kRxDepth = 4;
constexpr UBaseType_t kTxDepth = 8;
constexpr uint32_t kPumpPeriodMs = 100;

esp_mqtt_client_handle_t g_client = nullptr;
QueueHandle_t g_rx = nullptr;
QueueHandle_t g_tx = nullptr;

// Written by one task each, read by others; atomics rather than volatile so the
// counters do not tear.
std::atomic<bool> g_started{false};    // pump task
std::atomic<bool> g_connected{false};  // esp-mqtt task
std::atomic<uint8_t> g_last_state{0};  // a PilotState as int; A after reset
std::atomic<uint32_t> g_ignored_rx{0};
std::atomic<uint32_t> g_dropped_rx{0};
std::atomic<uint32_t> g_dropped_tx{0};

// Runs in the esp-mqtt task. Must stay short and must not touch Serial.
esp_err_t on_event(esp_mqtt_event_handle_t event) {
  switch (event->event_id) {
    case MQTT_EVENT_CONNECTED: {
      g_connected = true;
      esp_mqtt_client_subscribe(event->client, kMqttTopicCmdState, 1);
      esp_mqtt_client_publish(event->client, kMqttTopicStatus, "online", 0, 1, 1);
      const char *name =
          pilot_state_name(static_cast<PilotState>(g_last_state.load()));
      esp_mqtt_client_publish(event->client, kMqttTopicState, name, 0, 1, 1);
      break;
    }
    case MQTT_EVENT_DISCONNECTED:
      g_connected = false;
      break;
    case MQTT_EVENT_DATA: {
      const bool has_nul =
          event->data_len > 0 && event->data != nullptr &&
          memchr(event->data, '\0', event->data_len) != nullptr;
      const MqttRx verdict =
          mqtt_rx_filter(event->retain, event->data_len, event->total_data_len,
                         event->current_data_offset, has_nul);
      if (verdict != MqttRx::Accept) {
        g_ignored_rx++;
        break;
      }
      RxItem item;
      memcpy(item.text, event->data, event->data_len);
      item.text[event->data_len] = '\0';
      if (xQueueSend(g_rx, &item, 0) != pdTRUE) {
        g_dropped_rx++;
      }
      break;
    }
    default:
      break;
  }
  return ESP_OK;
}

// Owns the client's lifecycle and every publish. It may block inside esp-mqtt
// for as long as it likes; loop() never waits for it.
void pump_task(void *) {
  for (;;) {
    TxItem item;
    if (xQueueReceive(g_tx, &item, pdMS_TO_TICKS(kPumpPeriodMs)) == pdTRUE) {
      if (g_connected) {
        const bool is_state = item.kind == TxKind::State;
        esp_mqtt_client_enqueue(g_client,
                                is_state ? kMqttTopicState : kMqttTopicReply,
                                item.text, 0, 1, is_state ? 1 : 0, false);
      } else {
        g_dropped_tx++;
      }
    }

    const bool wifi_up = WiFi.status() == WL_CONNECTED;
    if (wifi_up && !g_started) {
      if (esp_mqtt_client_start(g_client) == ESP_OK) {
        g_started = true;
      }
    } else if (!wifi_up && g_started) {
      esp_mqtt_client_stop(g_client);
      g_started = false;
      g_connected = false;
    }
  }
}

void push_tx(TxKind kind, const char *text) {
  if (g_tx == nullptr || text == nullptr) {
    return;
  }
  TxItem item;
  item.kind = kind;
  snprintf(item.text, sizeof item.text, "%s", text);
  if (xQueueSend(g_tx, &item, 0) != pdTRUE) {
    g_dropped_tx++;
  }
}

}  // namespace

void net_mqtt_begin() {
  g_rx = xQueueCreate(kRxDepth, sizeof(RxItem));
  g_tx = xQueueCreate(kTxDepth, sizeof(TxItem));

  esp_mqtt_client_config_t config = {};
  config.uri = kMqttBrokerUri;
  config.client_id = kMqttClientId;
  config.event_handle = on_event;
  config.lwt_topic = kMqttTopicStatus;
  config.lwt_msg = "offline";
  config.lwt_msg_len = sizeof("offline") - 1;
  config.lwt_qos = 1;
  config.lwt_retain = 1;
  config.keepalive = kMqttKeepaliveS;
  config.reconnect_timeout_ms = kMqttReconnectMs;
  g_client = esp_mqtt_client_init(&config);

  if (g_rx == nullptr || g_tx == nullptr || g_client == nullptr ||
      xTaskCreatePinnedToCore(pump_task, "mqtt_pump", 4096, nullptr, 1, nullptr,
                              0) != pdPASS) {
    Serial.println("# net_mqtt: init failed -- MQTT disabled");
    g_client = nullptr;
    g_rx = nullptr;
    g_tx = nullptr;
  }
}

bool net_mqtt_take_command(char *out, size_t capacity) {
  if (g_rx == nullptr || out == nullptr || capacity < kMqttCommandCapacity) {
    return false;
  }
  RxItem item;
  if (xQueueReceive(g_rx, &item, 0) != pdTRUE) {
    return false;
  }
  memcpy(out, item.text, sizeof item.text);
  return true;
}

void net_mqtt_publish_state(PilotState state) {
  g_last_state = static_cast<uint8_t>(state);
  push_tx(TxKind::State, pilot_state_name(state));
}

void net_mqtt_publish_reply(const char *text) {
  push_tx(TxKind::Reply, text);
}

void net_mqtt_print_status() {
  if (g_client == nullptr) {
    Serial.println("# net_mqtt: disabled (init failed)");
    return;
  }
  Serial.print("# net_mqtt: broker ");
  Serial.print(kMqttBrokerUri);
  Serial.print(", client ");
  Serial.print(g_started ? "started" : "stopped");
  Serial.print(", ");
  Serial.println(g_connected ? "connected" : "not connected");
  Serial.print("# net_mqtt: ignored rx ");
  Serial.print(g_ignored_rx.load());
  Serial.print(", dropped rx ");
  Serial.print(g_dropped_rx.load());
  Serial.print(", dropped tx ");
  Serial.println(g_dropped_tx.load());
}
