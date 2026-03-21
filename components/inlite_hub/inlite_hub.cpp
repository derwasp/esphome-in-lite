#include "inlite_hub.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <string>

#include <esp_bt_defs.h>
#include <esp_err.h>
#include <esp_gap_ble_api.h>
#include <esp_gatt_common_api.h>
#include <esp_random.h>
#include <mbedtls/cipher.h>
#include <mbedtls/md.h>

#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "esphome/core/log.h"

namespace esphome {
namespace inlite_hub {

static const char *const TAG = "inlite_hub";

static const espbt::ESPBTUUID kMeshServiceUuid =
    espbt::ESPBTUUID::from_raw("0000fef1-0000-1000-8000-00805f9b34fb");
static const espbt::ESPBTUUID kContinuationNotifyUuid =
    espbt::ESPBTUUID::from_raw("c4edc000-9daf-11e3-8003-00025b000b00");
static const espbt::ESPBTUUID kCompleteNotifyUuid =
    espbt::ESPBTUUID::from_raw("c4edc000-9daf-11e3-8004-00025b000b00");
static const espbt::ESPBTUUID kContinuationWriteUuid =
    espbt::ESPBTUUID::from_raw("c4edc000-9daf-11e3-8003-10025b000b00");
static const espbt::ESPBTUUID kCompleteWriteUuid =
    espbt::ESPBTUUID::from_raw("c4edc000-9daf-11e3-8004-10025b000b00");

static inline bool output_mode_is_on(uint8_t output_mode) { return (output_mode & 0x01) != 0; }

void InliteHub::setup() {
  // Mirror the app defaults: random controller id in [32768..65533] and random 24-bit sequence.
  this->controller_id_ = static_cast<uint16_t>(32768 + (esp_random() % (65533 - 32768 + 1)));
  this->sequence_number_ = esp_random() & 0x00FFFFFF;

  std::vector<uint8_t> passphrase_bytes;
  if (!this->decode_hex_string_(this->network_passphrase_hex_, passphrase_bytes)) {
    ESP_LOGE(TAG, "Invalid network_passphrase_hex; cannot initialize inlite_hub");
    this->mark_failed();
    return;
  }

  this->derive_network_key_(passphrase_bytes);
  if (this->parent() != nullptr && this->auto_discover_) {
    this->selected_discovery_address_ = this->parent()->get_address();
  }
  this->reset_ble_state_();

  esp_err_t mtu_status = esp_ble_gatt_set_local_mtu(81);
  if (mtu_status != ESP_OK) {
    ESP_LOGW(TAG, "Failed to set local MTU to 81 (status=%d)", mtu_status);
  }

  this->publish_connected_state_();
}

void InliteHub::dump_config() {
  ESP_LOGCONFIG(TAG, "in-lite Smart Hub-150 BLE Bridge:");
  ESP_LOGCONFIG(TAG, "  Hub ID: %u", this->hub_id_);
  ESP_LOGCONFIG(TAG, "  Controller ID: %u", this->controller_id_);
  ESP_LOGCONFIG(TAG, "  Network passphrase: configured via hex (%u chars)",
                static_cast<unsigned int>(this->network_passphrase_hex_.size()));
  ESP_LOGCONFIG(TAG, "  Auto-discovery: %s", this->auto_discover_ ? "yes" : "no");
  if (this->auto_discover_) {
    ESP_LOGCONFIG(TAG, "  Discovery name filter: %s",
                  this->discover_name_filter_.empty() ? "(none)" : this->discover_name_filter_.c_str());
    if (this->discover_match_address_ != 0) {
      ESP_LOGCONFIG(TAG, "  Discovery preferred address: 0x%012llx",
                    static_cast<unsigned long long>(this->discover_match_address_));
    }
  }
  ESP_LOGCONFIG(TAG, "  Command timeout: %ums", this->command_timeout_ms_);
  ESP_LOGCONFIG(TAG, "  Retries: %u", this->retries_);
  ESP_LOGCONFIG(TAG, "  Connected: %s", this->last_connected_state_ ? "yes" : "no");

  if (this->hub_id_ == 0) {
    ESP_LOGW(TAG, "Hub ID is 0; configure `hub_id` to your Smart Hub mesh device ID.");
  }
}

void InliteHub::register_line_light(InliteLineLight *line_light) {
  this->line_lights_.push_back(line_light);
}

void InliteHub::queue_line_command(uint8_t line_id, bool on, uint8_t brightness) {
  std::vector<uint8_t> mode_cmd = {
      kCmdTypeRequest,
      static_cast<uint8_t>(kOpcodeSetOutletMode & 0xFF),
      static_cast<uint8_t>((kOpcodeSetOutletMode >> 8) & 0xFF),
      line_id,
      static_cast<uint8_t>(on ? 0x01 : 0x00),
      0x01,
  };
  this->queue_.push_back({mode_cmd});

  // Brightness framing is inferred from static APK analysis and must be validated on hardware.
  if (on) {
    std::vector<uint8_t> brightness_cmd = {
        kCmdTypeRequest,
        static_cast<uint8_t>(kOpcodeSetOutletBrightness & 0xFF),
        static_cast<uint8_t>((kOpcodeSetOutletBrightness >> 8) & 0xFF),
        line_id,
        brightness,
    };
    this->queue_.push_back({brightness_cmd});
  }
}

void InliteHub::queue_state_sync_request_(bool force) {
  if (this->node_state != espbt::ClientState::ESTABLISHED || !this->characteristics_ready_) {
    return;
  }
  if (!force && this->has_received_state_snapshot_) {
    return;
  }

  std::vector<uint8_t> get_info_cmd = {
      kCmdTypeRequest,
      static_cast<uint8_t>(kOpcodeGetInfoDevices & 0xFF),
      static_cast<uint8_t>((kOpcodeGetInfoDevices >> 8) & 0xFF),
  };
  auto payload_is_get_info = [&](const std::vector<uint8_t> &payload) {
    return payload.size() == get_info_cmd.size() &&
           std::equal(payload.begin(), payload.end(), get_info_cmd.begin());
  };

  if (this->active_stream_.active && payload_is_get_info(this->active_stream_.payload)) {
    return;
  }
  for (const auto &queued : this->queue_) {
    if (payload_is_get_info(queued.payload)) {
      return;
    }
  }

  uint32_t now = millis();
  if (!force) {
    uint32_t min_gap_ms = std::max<uint32_t>(this->get_update_interval(), 1000);
    if (now - this->last_state_sync_request_ms_ < min_gap_ms) {
      return;
    }
  }

  this->queue_.push_back({get_info_cmd});
  this->last_state_sync_request_ms_ = now;
  ESP_LOGD(TAG, "Queued state sync request (GET_INFO_DEVICES)");
}

void InliteHub::on_scan_end() {
  if (!this->auto_discover_) {
    return;
  }
  // Slowly decay candidate score so a rotated random address can be selected later.
  this->selected_discovery_score_ = std::max(-10000, this->selected_discovery_score_ - 2);
}

bool InliteHub::parse_device(const espbt::ESPBTDevice &device) {
  if (!this->auto_discover_ || this->parent() == nullptr) {
    return false;
  }

  if (this->parent()->state() != espbt::ClientState::IDLE &&
      this->parent()->state() != espbt::ClientState::DISCOVERED) {
    return false;
  }

  bool service_hit = false;
  for (const auto &service_uuid : device.get_service_uuids()) {
    if (service_uuid == kMeshServiceUuid) {
      service_hit = true;
      break;
    }
  }

  const std::string &name = device.get_name();
  bool name_hit = this->contains_case_insensitive_(name, this->discover_name_filter_);
  uint64_t candidate_address = device.address_uint64();
  bool match_hit =
      this->discover_match_address_ != 0 && candidate_address == this->discover_match_address_;

  if (!(match_hit || service_hit || name_hit)) {
    return false;
  }

  int candidate_score =
      this->autodiscovery_score_(match_hit, service_hit, name_hit, device.get_rssi());
  if (!this->should_select_candidate_(candidate_address, candidate_score)) {
    return true;
  }

  bool changed = candidate_address != this->selected_discovery_address_;
  this->selected_discovery_address_ = candidate_address;
  this->selected_discovery_score_ = candidate_score;

  this->parent()->set_address(candidate_address);
  this->parent()->set_remote_addr_type(device.get_address_type());
  if (this->parent()->state() == espbt::ClientState::IDLE) {
    this->parent()->set_state(espbt::ClientState::DISCOVERED);
  }

  if (changed) {
    ESP_LOGI(TAG, "Autodiscovery selected %s (rssi=%d, service_hit=%s, name_hit=%s, match_hit=%s)",
             device.address_str().c_str(), device.get_rssi(), service_hit ? "true" : "false",
             name_hit ? "true" : "false", match_hit ? "true" : "false");
  }

  return true;
}

void InliteHub::loop() {
  bool connected_now = this->node_state == espbt::ClientState::ESTABLISHED && this->characteristics_ready_;
  bool was_connected = this->last_connected_state_;
  if (connected_now != was_connected) {
    this->last_connected_state_ = connected_now;
    this->publish_connected_state_();
  }
  if (connected_now && !was_connected) {
    this->queue_state_sync_request_(true);
  }

  if (!connected_now) {
    return;
  }

  this->process_active_stream_();
}

void InliteHub::update() {
  this->request_rssi_();
  this->queue_state_sync_request_(false);
}

void InliteHub::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                    esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_OPEN_EVT:
      if (param->open.status == ESP_GATT_OK) {
        ESP_LOGI(TAG, "Connected to %s", this->parent()->address_str());
      }
      break;

    case ESP_GATTC_CLOSE_EVT:
      ESP_LOGW(TAG, "Disconnected from %s", this->parent()->address_str());
      this->reset_ble_state_();
      this->publish_connected_state_();
      break;

    case ESP_GATTC_SEARCH_CMPL_EVT:
      this->reset_ble_state_();
      if (this->configure_characteristics_()) {
        ESP_LOGI(TAG, "Mesh service discovered; registering notifications");
      }
      break;

    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (param->reg_for_notify.status != ESP_GATT_OK) {
        ESP_LOGW(TAG, "Failed registering for notify handle=0x%x status=%d",
                 param->reg_for_notify.handle, param->reg_for_notify.status);
        break;
      }
      if (param->reg_for_notify.handle == this->continuation_notify_handle_) {
        this->continuation_notify_registered_ = true;
      }
      if (param->reg_for_notify.handle == this->complete_notify_handle_) {
        this->complete_notify_registered_ = true;
      }
      if (this->continuation_notify_registered_ && this->complete_notify_registered_) {
        this->characteristics_ready_ = true;
        this->node_state = espbt::ClientState::ESTABLISHED;
        ESP_LOGI(TAG, "in-lite notify channels ready");
      }
      break;

    case ESP_GATTC_NOTIFY_EVT: {
      const uint8_t *value = param->notify.value;
      uint16_t value_len = param->notify.value_len;

      if (param->notify.handle == this->continuation_notify_handle_) {
        this->incoming_packet_buffer_.insert(this->incoming_packet_buffer_.end(), value,
                                             value + value_len);
      } else if (param->notify.handle == this->complete_notify_handle_) {
        this->incoming_packet_buffer_.insert(this->incoming_packet_buffer_.end(), value,
                                             value + value_len);

        MeshPacket packet;
        if (this->decrypt_packet_(this->incoming_packet_buffer_, packet)) {
          this->handle_mesh_packet_(packet);
        }
        this->incoming_packet_buffer_.clear();
      }
      break;
    }

    default:
      break;
  }
}

void InliteHub::gap_event_handler(esp_gap_ble_cb_event_t event,
                                  esp_ble_gap_cb_param_t *param) {
  if (event != ESP_GAP_BLE_READ_RSSI_COMPLETE_EVT) {
    return;
  }

  if (param->read_rssi_cmpl.status != ESP_BT_STATUS_SUCCESS) {
    ESP_LOGW(TAG, "RSSI read failed with status=%d", param->read_rssi_cmpl.status);
    return;
  }

  if (this->rssi_sensor_ != nullptr) {
    this->rssi_sensor_->publish_state(static_cast<float>(param->read_rssi_cmpl.rssi));
  }
}

void InliteHub::reset_ble_state_() {
  this->characteristics_ready_ = false;
  this->continuation_notify_registered_ = false;
  this->complete_notify_registered_ = false;

  this->continuation_notify_handle_ = 0;
  this->complete_notify_handle_ = 0;
  this->continuation_write_handle_ = 0;
  this->complete_write_handle_ = 0;

  this->incoming_packet_buffer_.clear();
  this->active_stream_ = {};
  this->has_received_state_snapshot_ = false;
  this->last_state_sync_request_ms_ = 0;
}

bool InliteHub::configure_characteristics_() {
  auto *continuation_notify_chr =
      this->parent()->get_characteristic(kMeshServiceUuid, kContinuationNotifyUuid);
  auto *complete_notify_chr =
      this->parent()->get_characteristic(kMeshServiceUuid, kCompleteNotifyUuid);
  auto *continuation_write_chr =
      this->parent()->get_characteristic(kMeshServiceUuid, kContinuationWriteUuid);
  auto *complete_write_chr =
      this->parent()->get_characteristic(kMeshServiceUuid, kCompleteWriteUuid);

  if (continuation_notify_chr == nullptr || complete_notify_chr == nullptr ||
      continuation_write_chr == nullptr || complete_write_chr == nullptr) {
    ESP_LOGE(TAG, "Missing required mesh characteristics on %s",
             this->parent()->address_str());
    this->publish_last_command_status_(-20);
    return false;
  }

  this->continuation_notify_handle_ = continuation_notify_chr->handle;
  this->complete_notify_handle_ = complete_notify_chr->handle;
  this->continuation_write_handle_ = continuation_write_chr->handle;
  this->complete_write_handle_ = complete_write_chr->handle;

  esp_err_t st1 = esp_ble_gattc_register_for_notify(
      this->parent()->get_gattc_if(), this->parent()->get_remote_bda(),
      this->continuation_notify_handle_);
  if (st1 != ESP_OK) {
    ESP_LOGE(TAG, "register_for_notify continuation failed status=%d", st1);
    this->publish_last_command_status_(-21);
    return false;
  }

  esp_err_t st2 = esp_ble_gattc_register_for_notify(
      this->parent()->get_gattc_if(), this->parent()->get_remote_bda(),
      this->complete_notify_handle_);
  if (st2 != ESP_OK) {
    ESP_LOGE(TAG, "register_for_notify complete failed status=%d", st2);
    this->publish_last_command_status_(-22);
    return false;
  }

  return true;
}

void InliteHub::process_active_stream_() {
  if (!this->active_stream_.active) {
    if (this->queue_.empty()) {
      return;
    }

    this->active_stream_.active = true;
    this->active_stream_.stage = StreamStage::SEND_START;
    this->active_stream_.payload = this->queue_.front().payload;
    this->active_stream_.offset = 0;
    this->active_stream_.attempts = 0;
    this->active_stream_.expected_ack = 0;
    this->queue_.pop_front();
  }

  switch (this->active_stream_.stage) {
    case StreamStage::SEND_START: {
      std::vector<uint8_t> start_data = {0x00, 0x00};
      if (!this->send_stream_packet_(kPktTypeStartFlush, start_data)) {
        this->finish_active_stream_(-30);
        return;
      }
      this->active_stream_.expected_ack = 0;
      this->active_stream_.stage = StreamStage::WAIT_START_ACK;
      this->active_stream_.stage_started_ms = millis();
      return;
    }

    case StreamStage::SEND_DATA: {
      if (this->active_stream_.offset >= this->active_stream_.payload.size()) {
        this->active_stream_.stage = StreamStage::SEND_END;
        return;
      }

      size_t remaining = this->active_stream_.payload.size() - this->active_stream_.offset;
      size_t chunk_len = std::min(remaining, kMaxDataChunk);

      std::vector<uint8_t> data;
      data.reserve(2 + chunk_len);
      data.push_back(static_cast<uint8_t>(this->active_stream_.offset & 0xFF));
      data.push_back(static_cast<uint8_t>((this->active_stream_.offset >> 8) & 0xFF));
      data.insert(data.end(), this->active_stream_.payload.begin() + this->active_stream_.offset,
                  this->active_stream_.payload.begin() + this->active_stream_.offset + chunk_len);

      if (!this->send_stream_packet_(kPktTypeData, data)) {
        this->finish_active_stream_(-31);
        return;
      }

      this->active_stream_.expected_ack =
          static_cast<uint16_t>(this->active_stream_.offset + chunk_len);
      this->active_stream_.stage = StreamStage::WAIT_DATA_ACK;
      this->active_stream_.stage_started_ms = millis();
      return;
    }

    case StreamStage::SEND_END: {
      uint16_t final_offset = static_cast<uint16_t>(this->active_stream_.payload.size());
      std::vector<uint8_t> end_data = {
          static_cast<uint8_t>(final_offset & 0xFF),
          static_cast<uint8_t>((final_offset >> 8) & 0xFF),
      };

      if (!this->send_stream_packet_(kPktTypeStartFlush, end_data)) {
        this->finish_active_stream_(-32);
        return;
      }

      this->active_stream_.expected_ack = final_offset;
      this->active_stream_.stage = StreamStage::WAIT_END_ACK;
      this->active_stream_.stage_started_ms = millis();
      return;
    }

    case StreamStage::WAIT_START_ACK:
    case StreamStage::WAIT_DATA_ACK:
    case StreamStage::WAIT_END_ACK:
      if (millis() - this->active_stream_.stage_started_ms > this->command_timeout_ms_) {
        this->retry_or_fail_();
      }
      return;

    case StreamStage::IDLE:
      return;
  }
}

bool InliteHub::retry_or_fail_() {
  if (this->active_stream_.attempts >= this->retries_) {
    this->finish_active_stream_(-40);
    return false;
  }

  this->active_stream_.attempts++;

  switch (this->active_stream_.stage) {
    case StreamStage::WAIT_START_ACK:
      this->active_stream_.stage = StreamStage::SEND_START;
      break;
    case StreamStage::WAIT_DATA_ACK:
      this->active_stream_.stage = StreamStage::SEND_DATA;
      break;
    case StreamStage::WAIT_END_ACK:
      this->active_stream_.stage = StreamStage::SEND_END;
      break;
    default:
      this->finish_active_stream_(-41);
      return false;
  }

  return true;
}

bool InliteHub::send_stream_packet_(uint8_t packet_type, const std::vector<uint8_t> &data) {
  std::vector<uint8_t> encrypted =
      this->build_encrypted_packet_(this->hub_id_, packet_type, data, kTtlDefault);
  if (encrypted.empty()) {
    return false;
  }
  return this->send_encrypted_packet_(encrypted);
}

bool InliteHub::send_encrypted_packet_(const std::vector<uint8_t> &encrypted_packet) {
  if (!this->characteristics_ready_ || this->parent() == nullptr ||
      this->continuation_write_handle_ == 0 || this->complete_write_handle_ == 0) {
    ESP_LOGW(TAG, "Cannot send mesh packet; BLE write characteristics not ready");
    return false;
  }

  for (size_t i = 0; i < encrypted_packet.size(); i += kBleChunkSize) {
    size_t chunk_len = std::min(kBleChunkSize, encrypted_packet.size() - i);
    bool is_last = (i + chunk_len) >= encrypted_packet.size();
    uint16_t handle = is_last ? this->complete_write_handle_ : this->continuation_write_handle_;

    esp_err_t status = esp_ble_gattc_write_char(
        this->parent()->get_gattc_if(), this->parent()->get_conn_id(), handle,
        static_cast<uint16_t>(chunk_len),
        const_cast<uint8_t *>(encrypted_packet.data() + i),
        ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);

    if (status != ESP_OK) {
      ESP_LOGW(TAG, "BLE write failed status=%d handle=0x%x", status, handle);
      return false;
    }
  }

  return true;
}

void InliteHub::handle_mesh_packet_(const MeshPacket &packet) {
  if (packet.source_id != this->hub_id_) {
    return;
  }

  if (packet.packet_type == kPktTypeAck) {
    if (packet.payload.size() < 2) {
      return;
    }
    uint16_t ack_offset = static_cast<uint16_t>(packet.payload[0] | (packet.payload[1] << 8));
    bool end_ack = packet.payload.size() >= 3 && packet.payload[2] == kEndAckMagic;
    this->handle_stream_ack_(ack_offset, end_ack);
    return;
  }

  if (packet.packet_type == kPktTypeBlockData) {
    this->handle_block_data_(packet.payload);
  }
}

void InliteHub::handle_block_data_(const std::vector<uint8_t> &payload) {
  if (payload.size() < 3) {
    return;
  }

  uint8_t cmd_type = payload[0];
  uint16_t opcode = static_cast<uint16_t>(payload[1] | (payload[2] << 8));
  if (cmd_type != kCmdTypeOob) {
    return;
  }

  if (opcode == kOpcodeOobOutletModeUpdate) {
    if (payload.size() < 6) {
      ESP_LOGW(TAG, "OOB single-line update too short (%u bytes)",
               static_cast<unsigned int>(payload.size()));
      return;
    }
    uint8_t output_rtc_timer = payload.size() >= 7 ? payload[6] : 0;
    this->has_received_state_snapshot_ = true;
    this->apply_line_mode_update_(payload[3], payload[4], payload[5], output_rtc_timer);
    return;
  }

  if (opcode != kOpcodeOobAllOutletsModeUpdate) {
    return;
  }

  if (payload.size() < 7) {
    return;
  }

  size_t body_len = payload.size() - 3;
  size_t line_chunks = body_len / 4;
  size_t trailing = body_len % 4;
  if (trailing != 0) {
    ESP_LOGW(TAG, "OOB all-lines payload has %u trailing byte(s)", static_cast<unsigned int>(trailing));
  }

  for (size_t i = 0; i < line_chunks; i++) {
    size_t base = 3 + (i * 4);
    this->apply_line_mode_update_(payload[base], payload[base + 1], payload[base + 2],
                                  payload[base + 3]);
  }
  if (line_chunks > 0) {
    this->has_received_state_snapshot_ = true;
  }
}

void InliteHub::apply_line_mode_update_(uint8_t line_id, uint8_t output_mode, uint8_t output_state,
                                        uint8_t output_rtc_timer) {
  ESP_LOGD(TAG, "line %u update mode=0x%02x state=0x%02x rtc=%u", line_id, output_mode, output_state,
           output_rtc_timer);
  for (auto *line_light : this->line_lights_) {
    if (line_light == nullptr || line_light->get_line() != line_id) {
      continue;
    }
    line_light->apply_remote_mode(output_mode, output_state, output_rtc_timer);
  }
}

void InliteHub::handle_stream_ack_(uint16_t ack_offset, bool end_ack) {
  if (!this->active_stream_.active) {
    return;
  }

  switch (this->active_stream_.stage) {
    case StreamStage::WAIT_START_ACK:
      if (end_ack) {
        return;
      }
      if (ack_offset != 0) {
        return;
      }
      this->active_stream_.offset = 0;
      this->active_stream_.stage = this->active_stream_.payload.empty() ? StreamStage::SEND_END
                                                                         : StreamStage::SEND_DATA;
      this->active_stream_.attempts = 0;
      return;

    case StreamStage::WAIT_DATA_ACK:
      if (end_ack) {
        return;
      }
      if (ack_offset != this->active_stream_.expected_ack) {
        return;
      }
      this->active_stream_.offset = ack_offset;
      this->active_stream_.stage = (this->active_stream_.offset < this->active_stream_.payload.size())
                                       ? StreamStage::SEND_DATA
                                       : StreamStage::SEND_END;
      this->active_stream_.attempts = 0;
      return;

    case StreamStage::WAIT_END_ACK:
      if (ack_offset != this->active_stream_.payload.size()) {
        return;
      }
      if (!end_ack) {
        // Regular ACK for final offset is also considered success by the Android app.
      }
      this->finish_active_stream_(0);
      return;

    default:
      return;
  }
}

void InliteHub::finish_active_stream_(int status_code) {
  this->publish_last_command_status_(status_code);
  this->active_stream_ = {};
}

std::vector<uint8_t> InliteHub::build_encrypted_packet_(uint16_t destination_id,
                                                        uint8_t packet_type,
                                                        const std::vector<uint8_t> &data,
                                                        uint8_t ttl) {
  this->sequence_number_ = (this->sequence_number_ + 1) & 0x00FFFFFF;

  std::vector<uint8_t> plain_payload;
  plain_payload.reserve(3 + data.size());
  plain_payload.push_back(static_cast<uint8_t>(destination_id & 0xFF));
  plain_payload.push_back(static_cast<uint8_t>((destination_id >> 8) & 0xFF));
  plain_payload.push_back(packet_type);
  plain_payload.insert(plain_payload.end(), data.begin(), data.end());

  auto iv = this->packet_iv_(this->sequence_number_, this->controller_id_);
  std::vector<uint8_t> encrypted_payload = this->aes_ofb_crypt_(plain_payload, this->network_key_, iv);
  if (encrypted_payload.size() != plain_payload.size()) {
    ESP_LOGW(TAG, "Failed to encrypt mesh payload");
    return {};
  }

  auto checksum = this->packet_checksum_(this->sequence_number_, this->controller_id_, encrypted_payload);

  std::vector<uint8_t> packet;
  packet.reserve(3 + 2 + encrypted_payload.size() + 8 + 1);
  packet.push_back(static_cast<uint8_t>(this->sequence_number_ & 0xFF));
  packet.push_back(static_cast<uint8_t>((this->sequence_number_ >> 8) & 0xFF));
  packet.push_back(static_cast<uint8_t>((this->sequence_number_ >> 16) & 0xFF));
  packet.push_back(static_cast<uint8_t>(this->controller_id_ & 0xFF));
  packet.push_back(static_cast<uint8_t>((this->controller_id_ >> 8) & 0xFF));
  packet.insert(packet.end(), encrypted_payload.begin(), encrypted_payload.end());
  packet.insert(packet.end(), checksum.begin(), checksum.end());
  packet.push_back(ttl);

  return packet;
}

bool InliteHub::decrypt_packet_(const std::vector<uint8_t> &encrypted_packet, MeshPacket &out) {
  if (encrypted_packet.size() < 14) {
    return false;
  }

  uint32_t sequence = static_cast<uint32_t>(encrypted_packet[0]) |
                      (static_cast<uint32_t>(encrypted_packet[1]) << 8) |
                      (static_cast<uint32_t>(encrypted_packet[2]) << 16);
  uint16_t source_id = static_cast<uint16_t>(encrypted_packet[3] | (encrypted_packet[4] << 8));
  uint8_t ttl = encrypted_packet.back();

  size_t checksum_start = encrypted_packet.size() - 9;
  std::array<uint8_t, 8> received_checksum{};
  std::copy_n(encrypted_packet.begin() + checksum_start, 8, received_checksum.begin());

  std::vector<uint8_t> encrypted_payload(encrypted_packet.begin() + 5,
                                         encrypted_packet.begin() + checksum_start);

  auto expected_checksum = this->packet_checksum_(sequence, source_id, encrypted_payload);
  if (!std::equal(expected_checksum.begin(), expected_checksum.end(), received_checksum.begin())) {
    return false;
  }

  auto iv = this->packet_iv_(sequence, source_id);
  std::vector<uint8_t> plain = this->aes_ofb_crypt_(encrypted_payload, this->network_key_, iv);
  if (plain.size() != encrypted_payload.size() || plain.size() < 3) {
    return false;
  }

  out.sequence = sequence;
  out.source_id = source_id;
  out.destination_id = static_cast<uint16_t>(plain[0] | (plain[1] << 8));
  out.packet_type = plain[2];
  out.ttl = ttl;
  out.payload.assign(plain.begin() + 3, plain.end());
  return true;
}

void InliteHub::derive_network_key_(const std::vector<uint8_t> &passphrase_bytes) {
  std::vector<uint8_t> seed(passphrase_bytes.begin(), passphrase_bytes.end());
  seed.push_back(0x00);
  seed.push_back('M');
  seed.push_back('C');
  seed.push_back('P');

  std::array<uint8_t, 32> digest{};
  const mbedtls_md_info_t *md_info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md(md_info, seed.data(), seed.size(), digest.data());

  for (size_t i = 0; i < this->network_key_.size(); i++) {
    this->network_key_[i] = digest[digest.size() - 1 - i];
  }
}

bool InliteHub::decode_hex_string_(const std::string &hex, std::vector<uint8_t> &out_bytes) {
  std::string cleaned;
  cleaned.reserve(hex.size());

  size_t idx = 0;
  if (hex.size() >= 2 && hex[0] == '0' && (hex[1] == 'x' || hex[1] == 'X')) {
    idx = 2;
  }

  for (; idx < hex.size(); idx++) {
    char c = hex[idx];
    if (std::isspace(static_cast<unsigned char>(c)) || c == ':' || c == '-') {
      continue;
    }
    cleaned.push_back(c);
  }

  if (cleaned.empty() || (cleaned.size() % 2) != 0) {
    return false;
  }

  auto hex_nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') {
      return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
      return 10 + (c - 'a');
    }
    if (c >= 'A' && c <= 'F') {
      return 10 + (c - 'A');
    }
    return -1;
  };

  out_bytes.clear();
  out_bytes.reserve(cleaned.size() / 2);
  for (size_t i = 0; i < cleaned.size(); i += 2) {
    int hi = hex_nibble(cleaned[i]);
    int lo = hex_nibble(cleaned[i + 1]);
    if (hi < 0 || lo < 0) {
      out_bytes.clear();
      return false;
    }
    out_bytes.push_back(static_cast<uint8_t>((hi << 4) | lo));
  }

  return true;
}

bool InliteHub::contains_case_insensitive_(const std::string &haystack,
                                           const std::string &needle) const {
  if (needle.empty()) {
    return false;
  }

  std::string haystack_lower(haystack);
  std::string needle_lower(needle);

  std::transform(haystack_lower.begin(), haystack_lower.end(), haystack_lower.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  std::transform(needle_lower.begin(), needle_lower.end(), needle_lower.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

  return haystack_lower.find(needle_lower) != std::string::npos;
}

int InliteHub::autodiscovery_score_(bool match_hit, bool service_hit, bool name_hit,
                                    int rssi) const {
  int score = 0;
  if (match_hit) {
    score += 10000;
  }
  if (service_hit) {
    score += 1000;
  }
  if (name_hit) {
    score += 100;
  }
  score += std::clamp(rssi, -100, 0);
  return score;
}

bool InliteHub::should_select_candidate_(uint64_t candidate_address, int candidate_score) const {
  if (this->selected_discovery_address_ == 0 || candidate_address == this->selected_discovery_address_) {
    return true;
  }

  if (this->discover_match_address_ != 0) {
    if (candidate_address == this->discover_match_address_) {
      return true;
    }
    if (this->selected_discovery_address_ == this->discover_match_address_) {
      return false;
    }
  }

  if (candidate_score > this->selected_discovery_score_) {
    return true;
  }
  if (candidate_score == this->selected_discovery_score_ &&
      candidate_address > this->selected_discovery_address_) {
    return true;
  }

  return false;
}

bool InliteHub::hmac_sha256_(const uint8_t *data, size_t data_len, const uint8_t *key,
                             size_t key_len, uint8_t *out_digest) {
  const mbedtls_md_info_t *md_info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  return mbedtls_md_hmac(md_info, key, key_len, data, data_len, out_digest) == 0;
}

std::array<uint8_t, 8> InliteHub::packet_checksum_(uint32_t sequence, uint16_t source_id,
                                                   const std::vector<uint8_t> &encrypted_payload) {
  std::vector<uint8_t> checksum_input;
  checksum_input.reserve(8 + 3 + 2 + encrypted_payload.size());
  checksum_input.insert(checksum_input.end(), 8, 0x00);
  checksum_input.push_back(static_cast<uint8_t>(sequence & 0xFF));
  checksum_input.push_back(static_cast<uint8_t>((sequence >> 8) & 0xFF));
  checksum_input.push_back(static_cast<uint8_t>((sequence >> 16) & 0xFF));
  checksum_input.push_back(static_cast<uint8_t>(source_id & 0xFF));
  checksum_input.push_back(static_cast<uint8_t>((source_id >> 8) & 0xFF));
  checksum_input.insert(checksum_input.end(), encrypted_payload.begin(), encrypted_payload.end());

  std::array<uint8_t, 32> digest{};
  this->hmac_sha256_(checksum_input.data(), checksum_input.size(), this->network_key_.data(),
                     this->network_key_.size(), digest.data());

  std::array<uint8_t, 8> checksum{};
  for (size_t i = 0; i < checksum.size(); i++) {
    checksum[i] = digest[digest.size() - 1 - i];
  }
  return checksum;
}

std::array<uint8_t, 16> InliteHub::packet_iv_(uint32_t sequence, uint16_t source_id) const {
  std::array<uint8_t, 16> iv{};
  iv[0] = static_cast<uint8_t>(sequence & 0xFF);
  iv[1] = static_cast<uint8_t>((sequence >> 8) & 0xFF);
  iv[2] = static_cast<uint8_t>((sequence >> 16) & 0xFF);
  iv[3] = 0x00;
  iv[4] = static_cast<uint8_t>(source_id & 0xFF);
  iv[5] = static_cast<uint8_t>((source_id >> 8) & 0xFF);
  return iv;
}

std::vector<uint8_t> InliteHub::aes_ofb_crypt_(const std::vector<uint8_t> &input,
                                               const std::array<uint8_t, 16> &key,
                                               const std::array<uint8_t, 16> &iv) {
  std::vector<uint8_t> output(input.size() + 16);
  size_t output_len = 0;

  mbedtls_cipher_context_t ctx;
  mbedtls_cipher_init(&ctx);

  const mbedtls_cipher_info_t *cipher_info =
      mbedtls_cipher_info_from_type(MBEDTLS_CIPHER_AES_128_OFB);
  if (cipher_info == nullptr) {
    mbedtls_cipher_free(&ctx);
    return {};
  }

  if (mbedtls_cipher_setup(&ctx, cipher_info) != 0) {
    mbedtls_cipher_free(&ctx);
    return {};
  }

  if (mbedtls_cipher_setkey(&ctx, key.data(), 128, MBEDTLS_ENCRYPT) != 0) {
    mbedtls_cipher_free(&ctx);
    return {};
  }

  if (mbedtls_cipher_crypt(&ctx, iv.data(), iv.size(), input.data(), input.size(),
                           output.data(), &output_len) != 0) {
    mbedtls_cipher_free(&ctx);
    return {};
  }

  mbedtls_cipher_free(&ctx);
  output.resize(output_len);
  return output;
}

void InliteHub::request_rssi_() {
  if (this->node_state != espbt::ClientState::ESTABLISHED || !this->characteristics_ready_) {
    return;
  }

  esp_err_t status = esp_ble_gap_read_rssi(this->parent()->get_remote_bda());
  if (status != ESP_OK) {
    ESP_LOGW(TAG, "esp_ble_gap_read_rssi failed status=%d", status);
  }
}

void InliteHub::publish_connected_state_() {
  if (this->connected_binary_sensor_ != nullptr) {
    bool connected_now = this->node_state == espbt::ClientState::ESTABLISHED && this->characteristics_ready_;
    this->connected_binary_sensor_->publish_state(connected_now);
  }
}

void InliteHub::publish_last_command_status_(int status_code) {
  if (this->last_command_status_sensor_ != nullptr) {
    this->last_command_status_sensor_->publish_state(static_cast<float>(status_code));
  }
}

light::LightTraits InliteLineLight::get_traits() {
  light::LightTraits traits;
  traits.set_supported_color_modes({light::ColorMode::BRIGHTNESS});
  return traits;
}

void InliteLineLight::write_state(light::LightState *state) {
  this->state_ = state;

  bool on = state->current_values.is_on();
  float brightness = std::clamp(state->current_values.get_brightness(), 0.0f, 1.0f);
  uint8_t raw_brightness = static_cast<uint8_t>(std::round(brightness * 255.0f));

  if (on && raw_brightness > 0) {
    this->last_brightness_ = raw_brightness;
  }

  if (!on) {
    raw_brightness = 0;
  } else if (raw_brightness == 0) {
    raw_brightness = this->last_brightness_ > 0 ? this->last_brightness_ : 255;
  }

  if (this->parent_ != nullptr) {
    this->parent_->queue_line_command(this->line_, on, raw_brightness);
  }
}

void InliteLineLight::apply_remote_mode(uint8_t output_mode, uint8_t output_state,
                                        uint8_t output_rtc_timer) {
  this->last_output_mode_ = output_mode;
  this->last_output_state_ = output_state;
  this->last_output_rtc_timer_ = output_rtc_timer;

  bool on = output_mode_is_on(output_mode);
  if (on && this->last_brightness_ == 0) {
    this->last_brightness_ = 255;
  }

  if (this->state_ == nullptr) {
    return;
  }

  float brightness = on ? static_cast<float>(this->last_brightness_) / 255.0f : 0.0f;
  light::LightColorValues values = this->state_->remote_values;
  values.set_color_mode(light::ColorMode::BRIGHTNESS);
  values.set_state(on);
  values.set_brightness(brightness);
  this->state_->remote_values = values;
  this->state_->current_values = values;
  this->state_->publish_state();
}

}  // namespace inlite_hub
}  // namespace esphome
