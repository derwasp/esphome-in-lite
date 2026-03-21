# Quickstart

## 1) Add external component

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/derwasp/esphome-in-lite
      ref: main
    components: [inlite_hub]
```

## 2) Configure BLE

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

## 3) Configure hub

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
```

## 4) Add lines

```yaml
light:
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 1
    name: In-lite Line 1
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 2
    name: In-lite Line 2
  - platform: inlite_hub
    inlite_hub_id: inlite_hub_main
    line: 3
    name: In-lite Line 3
```

## 5) Validate

```bash
esphome config your_node.yaml
esphome compile your_node.yaml
```
