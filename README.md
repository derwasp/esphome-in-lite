# ESPHome in-lite (Smart Hub-150 BLE)

ESPHome external component for local BLE control of in-lite Smart Hub-150.

> Work in progress (WIP): this integration is not tested yet.

**Getting started:** make sure you already have an in-lite account configured in the mobile app (and your garden is visible there), then run `python3 tools/inlite_config_wizard.py` to log in, select a garden, and generate a ready-to-use ESPHome YAML with `hub_id` and `network_passphrase_hex`.

## What This Component Does

- Controls Smart Hub lines (`on/off`)
- Syncs line state from hub OOB updates (opcodes `24` and `33`)
- Exposes BLE diagnostics (RSSI, connection state, last command status)
- Supports runtime BLE autodiscovery on ESP32

## Quick Setup

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

2. Run the config wizard:

```bash
python3 tools/inlite_config_wizard.py
```

3. Compile your ESPHome config:

```bash
esphome config your_node.yaml
esphome compile your_node.yaml
```

## Required Inputs

- `hub_id` (mesh destination ID, for example `0x1234`)
- `network_passphrase_hex` (garden passphrase bytes as hex)

When autodiscovery is enabled, you do not need to provide the hub BLE address.

## Manual ESPHome Wiring

If you do not use the wizard, start from `examples/inlite_smart_hub_150_bridge.yaml` and set:

- `inlite_smart_hub_mesh_id`
- `inlite_smart_hub_passphrase_hex`

External component source:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/derwasp/esphome-in-lite
      ref: main
    components: [inlite_hub]
```

## Python Harness (Separate Test Bed)

`tools/inlite_ble_harness.py` is a local BLE protocol test bed. It runs on your computer and talks directly to the hub. It is useful for protocol debugging and connectivity checks, but it is not the ESP32 firmware and does not validate flash/runtime behavior on the ESP node.

Typical usage:

1. Self-check harness crypto/framing:

```bash
python3 tools/inlite_ble_harness.py selftest
```

2. Discover hub address candidates:

```bash
python3 tools/inlite_ble_harness.py scan --seconds 12 --name-filter inlite
```

3. Send a line command directly to the hub:

```bash
python3 tools/inlite_ble_harness.py \
  --hub-id 0x1234 \
  --passphrase-hex YOUR_HEX \
  line 0 on --auto-discover --discover-seconds 12
```

4. Query line states from hub updates.
   States are event-driven OOB updates, not a guaranteed on-demand read. `--trigger-get-info` can help, but the hub may still return nothing until a line changes. In practice, if you get an empty result, toggle a line in the official app (for example ON then OFF) or send a `line` command first, then run `query` again:

```bash
python3 tools/inlite_ble_harness.py \
  --hub-id 0x1234 \
  --passphrase-hex YOUR_HEX \
  query --auto-discover --trigger-get-info --listen-seconds 8 --json
```

## Repository Layout

- `components/inlite_hub/`: ESPHome external component
- `examples/inlite_smart_hub_150_bridge.yaml`: example node configuration
- `tools/inlite_config_wizard.py`: login + garden selection + YAML generation
- `tools/inlite_ble_harness.py`: local BLE test bed
- `docs/credentials.md`: credential retrieval flow
