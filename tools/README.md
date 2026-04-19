# Tools

This folder contains the local Python tooling used to inspect and exercise an in-lite Smart Hub over BLE.

## What The IDs Mean

- `hub_id`: the in-lite mesh destination ID for the Smart Hub, for example `0x1234`
- `passphrase_hex`: the garden network passphrase as raw bytes encoded as hex
- `mac`: the BLE address used to connect to the hub
  On macOS this is often a CoreBluetooth UUID rather than a colon-separated MAC.

`hub_id` is not the BLE address. The config wizard prints the correct `hub_id` for the selected garden.

## Setup

From the repository root:

```bash
python3 -m venv .venv
source ./activate.sh
pip install -r requirements-harness.txt
```

## Branch-stamped Firmware Builds

Use `tools/build_branch_firmware.py` when you want the ESPHome `project.version`
shown in Home Assistant to include the current git branch, for example
`0.9.0-codex-branch-version-build-script`.

Wrapper options such as `--branch` and `--base-version` can appear before or
after the optional ESPHome command. Pass extra ESPHome arguments after `--`.

Compile with the current branch name:

```bash
python3 tools/build_branch_firmware.py your_node.yaml
```

Upload with the current branch name:

```bash
python3 tools/build_branch_firmware.py your_node.yaml upload -- --device your_node.local
```

Override the base version or branch tag:

```bash
python3 tools/build_branch_firmware.py \
  your_node.yaml \
  --base-version 1.0.0 \
  --branch release-candidate
```

## Interactive Console

The live console keeps a connection open, shows connection and line state, lets you toggle lines `1` to `3`, and shows the BLE traffic log at the bottom.

It reads credentials from `--hub-id` / `--passphrase-hex` first, then falls back to `INLITE_HUB_ID` / `INLITE_PASSPHRASE_HEX`.

Run with environment variables:

```bash
export INLITE_HUB_ID=0x1234
export INLITE_PASSPHRASE_HEX=YOUR_PASSPHRASE_HEX
python3 tools/inlite_ble_console.py
```

Run with explicit arguments:

```bash
python3 tools/inlite_ble_console.py \
  --hub-id 0x1234 \
  --passphrase-hex YOUR_PASSPHRASE_HEX
```

If you already know the BLE address, add `--mac ...` to skip discovery.

Console keys:

- `C`: connect
- `D`: disconnect
- `1`, `2`, `3`: toggle lines 0, 1, and 2
- `R`: enqueue `GET_INFO_DEVICES` refresh
- `S`: rescan for the target hub
- `L`: clear the log
- `Q`: quit

## One-shot Harness

Use the harness for protocol testing without the curses UI.

Self-test:

```bash
python3 tools/inlite_ble_harness.py selftest
```

Scan for candidate hubs:

```bash
python3 tools/inlite_ble_harness.py scan --seconds 12 --name-filter inlite
```

Toggle a line:

```bash
python3 tools/inlite_ble_harness.py \
  --hub-id 0x1234 \
  --passphrase-hex YOUR_HEX \
  line 0 on --auto-discover
```
