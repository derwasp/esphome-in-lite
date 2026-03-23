# inlite_hub ESPHome External Component

External component for local BLE control of in-lite Smart Hub-150.

## External Component Wiring

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/derwasp/esphome-in-lite
      ref: main
    components: [inlite_hub]

esp32_ble_tracker:
  id: inlite_ble_tracker_id

ble_client:
  # Placeholder MAC; inlite_hub autodiscovery selects actual hub target at runtime.
  - id: inlite_ble_id
    mac_address: "00:00:00:00:00:00"
    auto_connect: false
```

## Public Interface

```yaml
inlite_hub:
  - id: inlite_hub_main
    ble_client_id: inlite_ble_id
    esp32_ble_id: inlite_ble_tracker_id
    hub_id: 0x1234
    network_passphrase_hex: "c3ce..."   # required, UTF-8 passphrase bytes as hex
    auto_discover: true
    discover_name_filter: "inlite"
    # discover_match_address: AA:BB:CC:DD:EE:FF
    command_timeout: 600ms
    retries: 2
    poll_interval: 15s
    state_refresh_interval: 5min
```

Child platforms:
- `light` platform `inlite_hub` with `line`
- `sensor` platform `inlite_hub` with `rssi` and `last_command_status`
- `binary_sensor` platform `inlite_hub` with `connected`

## Notes

- `network_passphrase_hex` is required and must contain the active garden passphrase bytes in hex.
- Autodiscovery selects candidates by preferred address (`discover_match_address`) and/or mesh service/name hits.
- In autodiscovery mode, user-specific runtime inputs are only `hub_id` and `network_passphrase_hex`.
- Line entities are updated from hub OOB line-mode packets (opcodes `24` and `33`).
- Local line toggles are published optimistically, then reconciled from hub OOB updates.
- During a short per-line pending window after a toggle, contradictory OOB updates are ignored to prevent HA toggle bounce.
- On connect, and then every `poll_interval` until the first all-lines snapshot is seen, the component queues a state-sync request (`GET_INFO_DEVICES`, opcode `5`).
- After the first snapshot, periodic refresh requests continue every `state_refresh_interval` (default `5min`) for drift recovery.
