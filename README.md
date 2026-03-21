# ESPHome in-lite (Smart Hub-150 BLE)

ESPHome external component for local BLE control of in-lite Smart Hub-150.

> Work in progress (WIP): this integration is not tested yet and is still under active reverse-engineering.

Current scope (v1):
- Line control (on/off + brightness)
- Line state sync from hub OOB updates (opcodes 24/33)
- BLE diagnostics (RSSI, connection state, last command status)
- Runtime BLE autodiscovery on ESP32

## Repository Layout

- `components/inlite_hub/` -> external component
- `examples/inlite_smart_hub_150_bridge.yaml` -> full bridge example
- `tools/inlite_ble_harness.py` -> Python validator/debug harness
- `tools/inlite_config_wizard.py` -> interactive login + garden selection + YAML generator
- `docs/credentials.md` -> how to get required credentials (iPhone/macOS compatible)
- `docs/protocol-spec.md` -> protocol notes from reverse engineering

## Required Inputs

You need only:
- `hub_id` (mesh destination ID, for example `0x163E`)
- `network_passphrase_hex` (garden passphrase as UTF-8 bytes in hex)

Hub BLE MAC is optional when autodiscovery is enabled.

## Quick Start

0. Use a virtual environment (recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

1. Get `hub_id` and `network_passphrase_hex`:
- Follow `docs/credentials.md`.

Or run the interactive wizard:

```bash
python3 tools/inlite_config_wizard.py
```

At the end, the wizard can optionally verify connectivity by:
- discovering the hub
- turning lines `0,1,2` ON
- turning lines `0,1,2` OFF

2. Add external component to your ESPHome node:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/derwasp/esphome-in-lite
      ref: main
    components: [inlite_hub]
```

3. Use the bridge example:
- Start from `examples/inlite_smart_hub_150_bridge.yaml`.
- Set secrets:
  - `inlite_smart_hub_mesh_id`
  - `inlite_smart_hub_passphrase_hex`

4. Compile/flash:

```bash
esphome config your_node.yaml
esphome compile your_node.yaml
```

## Devcontainer

This repo includes a devcontainer with required build/runtime tooling:
- ESPHome CLI
- Python harness dependencies (`bleak`, `pycryptodomex`)
- system tools used by scripts/docs (`curl`, `jq`)

Use it:
1. Open the repo in VS Code.
2. Run `Dev Containers: Reopen in Container`.
3. Wait for post-create install to finish (`pip3 install -r requirements-dev.txt`).

## Testing

1. Validate ESPHome config/build:

```bash
esphome config your_node.yaml
esphome compile your_node.yaml
```

2. Install Python harness deps:

```bash
# if not already installed via requirements-dev.txt
pip install -r requirements-harness.txt
```

3. Run harness selftest:

```bash
python3 tools/inlite_ble_harness.py selftest
```

4. Run end-to-end BLE test (autodiscovery + send):

```bash
python3 tools/inlite_ble_harness.py \
  --hub-id 0x163E \
  --passphrase-hex YOUR_HEX \
  --timeout-ms 1200 \
  --retries 4 \
  --verbose \
  line 0 on --brightness 180 --auto-discover --discover-seconds 12
```

5. Query current line states from hub updates:

```bash
python3 tools/inlite_ble_harness.py \
  --hub-id 0x163E \
  --passphrase-hex YOUR_HEX \
  --timeout-ms 1200 \
  --retries 4 \
  --verbose \
  query --auto-discover --discover-seconds 12 --listen-seconds 6 --trigger-get-info
```

5. Test/setup wizard flow:

```bash
python3 tools/inlite_config_wizard.py --help
```

## Notes

- `4104` brightness payload is inferred and validated in practical use, but not officially documented by vendor.
- Do not commit real credentials/secrets to git.
