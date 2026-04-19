#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

#include <esp_gattc_api.h>

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/button/button.h"
#include "esphome/components/light/light_output.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

namespace esphome {
namespace inlite_hub {

namespace espbt = esphome::esp32_ble_tracker;

class InliteLineLight;
class InliteRefreshStateButton;
class InliteReconnectButton;

class InliteHub : public ble_client::BLEClientNode,
                  public PollingComponent,
                  public espbt::ESPBTDeviceListener {
 public:
  void setup() override;
  void loop() override;
  void update() override;
  void dump_config() override;
  bool parse_device(const espbt::ESPBTDevice &device) override;
  void on_scan_end() override;
  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;
  void gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_hub_id(uint16_t hub_id) { this->hub_id_ = hub_id; }
  void set_network_passphrase_hex(const std::string &passphrase_hex) {
    this->network_passphrase_hex_ = passphrase_hex;
  }
  void set_auto_discover(bool auto_discover) { this->auto_discover_ = auto_discover; }
  void set_discover_name_filter(const std::string &name_filter) {
    this->discover_name_filter_ = name_filter;
  }
  void set_discover_match_address(uint64_t discover_match_address) {
    this->discover_match_address_ = discover_match_address;
  }
  void set_command_timeout(uint32_t command_timeout_ms) {
    this->command_timeout_ms_ = command_timeout_ms;
  }
  void set_retries(uint8_t retries) { this->retries_ = retries; }
  void set_debug_transport(bool debug_transport) { this->debug_transport_ = debug_transport; }
  void set_state_refresh_interval(uint32_t state_refresh_interval_ms) {
    this->state_refresh_interval_ms_ = state_refresh_interval_ms;
  }

  void set_rssi_sensor(sensor::Sensor *sensor) { this->rssi_sensor_ = sensor; }
  void set_last_command_status_sensor(sensor::Sensor *sensor) {
    this->last_command_status_sensor_ = sensor;
  }
  void set_connected_binary_sensor(binary_sensor::BinarySensor *sensor) {
    this->connected_binary_sensor_ = sensor;
  }

  void register_line_light(InliteLineLight *line_light);
  void queue_line_command(uint8_t line_id, bool on);
  void debug_request_state_refresh();
  void debug_request_reconnect();

 protected:
  static constexpr uint8_t kCmdTypeRequest = 0x01;
  static constexpr uint16_t kOpcodeSetOutletMode = 4103;
  static constexpr uint16_t kOpcodeGetInfoDevices = 5;
  static constexpr uint8_t kPktTypeStartFlush = 112;
  static constexpr uint8_t kPktTypeData = 113;
  static constexpr uint8_t kPktTypeAck = 114;
  static constexpr uint8_t kPktTypeBlockData = 115;
  static constexpr uint8_t kPktTypeStreamDataAlt = 116;
  static constexpr uint8_t kCmdTypeOob = 0x03;
  static constexpr uint16_t kOpcodeOobOutletModeUpdate = 24;
  static constexpr uint16_t kOpcodeOobAllOutletsModeUpdate = 33;
  static constexpr uint8_t kProductTypeSmartHub = 3;
  static constexpr uint8_t kTtlDefault = 5;
  static constexpr uint8_t kEndAckMagic = 0xEF;
  static constexpr size_t kMaxDataChunk = 62;
  static constexpr size_t kBleChunkSize = 78;
  static constexpr uint32_t kRxStreamInactivityTimeoutMs = 15000;

  enum class StreamStage {
    IDLE,
    SEND_START,
    WAIT_START_ACK,
    SEND_DATA,
    WAIT_DATA_ACK,
    SEND_END,
    WAIT_END_ACK,
  };

  struct QueuedMeshPayload {
    std::vector<uint8_t> payload;
    bool is_line_command{false};
    uint8_t line_id{0};
    bool desired_on{false};
    uint32_t pending_token{0};
  };

  struct StreamState {
    bool active{false};
    StreamStage stage{StreamStage::IDLE};
    std::vector<uint8_t> payload;
    size_t offset{0};
    uint16_t expected_ack{0};
    uint8_t attempts{0};
    uint32_t stage_started_ms{0};
    bool is_line_command{false};
    uint8_t line_id{0};
    bool desired_on{false};
    uint32_t pending_token{0};
  };

  struct PendingLineState {
    bool active{false};
    bool desired_on{false};
    uint32_t started_ms{0};
    uint32_t token{0};
  };

  struct MeshPacket {
    uint32_t sequence{0};
    uint16_t source_id{0};
    uint16_t destination_id{0};
    uint8_t packet_type{0};
    uint8_t ttl{0};
    std::vector<uint8_t> payload;
  };

  struct QueuedTransportPacket {
    uint16_t destination_id{0};
    uint8_t packet_type{0};
    std::vector<uint8_t> payload;
  };

  struct ReverseStreamState {
    bool active{false};
    uint16_t source_id{0};
    uint16_t destination_id{0};
    std::vector<uint8_t> payload;
    uint16_t acked_bytes{0};
    bool completed{false};
    uint32_t last_activity_ms{0};
  };

  void reset_ble_state_(bool clear_pending);
  void clear_transport_runtime_(bool clear_command_queue, bool clear_pending_lines);
  bool configure_characteristics_();
  void process_rx_packet_queue_();
  void process_transport_packets_();
  bool process_pending_transport_packets_();
  void process_active_stream_();
  bool retry_or_fail_();
  void queue_state_sync_request_(bool force);
  void handle_reverse_stream_flush_(const MeshPacket &packet);
  void handle_reverse_stream_data_(const MeshPacket &packet);
  void queue_reverse_stream_ack_(uint16_t destination_id, uint16_t ack_offset, bool end_ack);
  void expire_reverse_stream_();
  void expire_stale_pending_lines_();
  uint32_t pending_line_timeout_ms_() const;
  uint32_t mark_line_pending_(uint8_t line_id, bool desired_on);
  void refresh_line_pending_started_ms_(uint8_t line_id, uint32_t pending_token);
  void clear_line_pending_(uint8_t line_id);
  bool get_pending_line_target_(uint8_t line_id, bool *desired_on);

  bool send_stream_packet_(uint8_t packet_type, const std::vector<uint8_t> &data);
  bool send_encrypted_packet_(const std::vector<uint8_t> &encrypted_packet);

  void handle_mesh_packet_(const MeshPacket &packet);
  void handle_block_data_(const std::vector<uint8_t> &payload);
  void handle_stream_ack_(uint16_t ack_offset, bool end_ack);
  bool parse_get_info_devices_payload_(const std::vector<uint8_t> &payload);
  bool is_get_info_devices_payload_(const std::vector<uint8_t> &payload) const;
  void apply_line_mode_update_(uint8_t line_id, uint8_t output_mode, uint8_t output_state,
                               uint8_t output_rtc_timer);
  void finish_active_stream_(int status_code);

  std::vector<uint8_t> build_encrypted_packet_(uint16_t destination_id,
                                               uint8_t packet_type,
                                               const std::vector<uint8_t> &data,
                                               uint8_t ttl);
  bool decrypt_packet_(const std::vector<uint8_t> &encrypted_packet, MeshPacket &out);

  void derive_network_key_(const std::vector<uint8_t> &passphrase_bytes);
  bool decode_hex_string_(const std::string &hex, std::vector<uint8_t> &out_bytes);
  bool contains_case_insensitive_(const std::string &haystack, const std::string &needle) const;
  int autodiscovery_score_(bool match_hit, bool service_hit, bool name_hit, int rssi) const;
  bool should_select_candidate_(uint64_t candidate_address, int candidate_score) const;
  bool hmac_sha256_(const uint8_t *data, size_t data_len, const uint8_t *key,
                    size_t key_len, uint8_t *out_digest);
  std::array<uint8_t, 8> packet_checksum_(uint32_t sequence, uint16_t source_id,
                                          const std::vector<uint8_t> &encrypted_payload);
  std::array<uint8_t, 16> packet_iv_(uint32_t sequence, uint16_t source_id) const;
  std::vector<uint8_t> aes_ofb_crypt_(const std::vector<uint8_t> &input,
                                      const std::array<uint8_t, 16> &key,
                                      const std::array<uint8_t, 16> &iv);

  void request_rssi_();
  void publish_connected_state_();
  void publish_last_command_status_(int status_code);

  uint16_t hub_id_{0};
  uint16_t controller_id_{0};
  uint32_t sequence_number_{0};
  std::string network_passphrase_hex_{};
  bool auto_discover_{true};
  std::string discover_name_filter_{"inlite"};
  uint64_t discover_match_address_{0};
  uint64_t selected_discovery_address_{0};
  int selected_discovery_score_{-10000};
  bool debug_transport_{false};

  uint32_t command_timeout_ms_{600};
  uint8_t retries_{2};
  uint32_t state_refresh_interval_ms_{300000};

  bool characteristics_ready_{false};
  bool continuation_notify_registered_{false};
  bool complete_notify_registered_{false};

  uint16_t continuation_notify_handle_{0};
  uint16_t complete_notify_handle_{0};
  uint16_t continuation_write_handle_{0};
  uint16_t complete_write_handle_{0};

  bool last_connected_state_{false};

  std::array<uint8_t, 16> network_key_{};

  std::vector<uint8_t> incoming_packet_buffer_;
  std::deque<MeshPacket> rx_packet_queue_;
  std::deque<QueuedTransportPacket> pending_transport_packets_;
  std::deque<QueuedMeshPayload> queue_;
  StreamState active_stream_;
  ReverseStreamState reverse_stream_;
  std::array<PendingLineState, 16> pending_line_states_{};
  uint32_t next_pending_token_{0};
  bool has_received_state_snapshot_{false};
  bool has_bootstrap_snapshot_{false};
  uint32_t last_state_sync_request_ms_{0};
  bool force_refresh_on_next_connect_{false};

  std::vector<InliteLineLight *> line_lights_;

  sensor::Sensor *rssi_sensor_{nullptr};
  sensor::Sensor *last_command_status_sensor_{nullptr};
  binary_sensor::BinarySensor *connected_binary_sensor_{nullptr};
};

class InliteLineLight : public light::LightOutput, public Component {
 public:
  void set_parent(InliteHub *parent) { this->parent_ = parent; }
  void set_line(uint8_t line) { this->line_ = line; }
  uint8_t get_line() const { return this->line_; }

  void setup_state(light::LightState *state) override { this->state_ = state; }
  light::LightTraits get_traits() override;
  void write_state(light::LightState *state) override;
  void apply_remote_mode(uint8_t output_mode, uint8_t output_state, uint8_t output_rtc_timer);

 protected:
  InliteHub *parent_{nullptr};
  light::LightState *state_{nullptr};
  uint8_t line_{0};
  uint8_t last_output_mode_{0};
  uint8_t last_output_state_{0};
  uint8_t last_output_rtc_timer_{0};
};

class InliteRefreshStateButton : public button::Button {
 public:
  void set_parent(InliteHub *parent) { this->parent_ = parent; }

 protected:
  void press_action() override;

  InliteHub *parent_{nullptr};
};

class InliteReconnectButton : public button::Button {
 public:
  void set_parent(InliteHub *parent) { this->parent_ = parent; }

 protected:
  void press_action() override;

  InliteHub *parent_{nullptr};
};

}  // namespace inlite_hub
}  // namespace esphome
