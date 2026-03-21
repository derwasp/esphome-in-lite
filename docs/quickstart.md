# Quickstart

## 0) Create and activate a venv (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## 1) Optional: generate a full YAML automatically

```bash
python3 tools/inlite_config_wizard.py
```

## 2) Add external component

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/derwasp/esphome-in-lite
      ref: main
    components: [inlite_hub]
```

## 3) Configure BLE

```yaml
esp32_ble:
  max_connections: 1

esp32_ble_tracker:
  id: inlite_ble_tracker_id
  scan_parameters:
    interval: 1100ms
    window: 1100ms
    active: false

ble_client:
  - mac_address: "00:00:00:00:00:00"
    id: inlite_ble_id
    auto_connect: false
```

## 4) Configure hub

```yaml
inlite_hub:
  - id: inlite_hub_main
    ble_client_id: inlite_ble_id
    esp32_ble_id: inlite_ble_tracker_id
    hub_id: !secret inlite_smart_hub_mesh_id
    network_passphrase_hex: !secret inlite_smart_hub_passphrase_hex
    auto_discover: true
    discover_name_filter: inlite
    command_timeout: 600ms
    retries: 2
    poll_interval: 15s
    state_refresh_interval: 5min
```

## 5) Add lines

```yaml
light:
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 0
    name: In-lite Line 0
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 1
    name: In-lite Line 1
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 2
    name: In-lite Line 2
```

## 6) Validate

```bash
esphome config your_node.yaml
esphome compile your_node.yaml
```

## 7) Test with Python harness (optional)

```bash
pip install -r requirements-harness.txt
python3 tools/inlite_ble_harness.py selftest
python3 tools/inlite_ble_harness.py \
  --hub-id 0x163E \
  --passphrase-hex YOUR_HEX \
  --timeout-ms 1200 \
  --retries 4 \
  --verbose \
  line 0 on --auto-discover --discover-seconds 12

python3 tools/inlite_ble_harness.py \
  --hub-id 0x163E \
  --passphrase-hex YOUR_HEX \
  --timeout-ms 1200 \
  --retries 4 \
  --verbose \
  query --auto-discover --discover-seconds 12 --listen-seconds 6 --trigger-get-info
```
