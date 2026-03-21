# in-lite Smart Hub-150 BLE Protocol Spec (FW 52, app 3.18.0)

## Scope

- Device target: `in-lite Smart Hub-150`
- Firmware target: `52`
- App artifact used for static reverse engineering: mirror package `nl.coffeeit.inliteapp` version `3.18.0` (`versionCode 452`)
- This spec captures protocol behavior required for line control (on/off + brightness) and BLE diagnostics.

## APK Trust Gate (Mirror First)

Mirror artifact was accepted only after identity checks:

- Extracted base APK: `/tmp/inlite_artifacts/nl.coffeeit.inliteapp.apk`
- Verified package name: `nl.coffeeit.inliteapp`
- Verified version: `versionName=3.18.0`, `versionCode=452`
- APK SHA-256:
  - `93ba866a0ede501357a622dad2592be14796bbf2e1df54fbdd8abceed8443f87`
- Signing cert fingerprints (`apksigner --print-certs`):
  - SHA-256: `da6dc38b74534e0d90fca76ecc6a252aca9d94b621c4dddbcc2cd6137da58484`
  - SHA-1: `541ea9315e214b777f29a5b1da8d9e2c750ef6cb`

If package name/version/cert fingerprint changes unexpectedly, stop and compare against a second source APK before reusing this spec.

## BLE GATT Layout

Mesh service UUID and characteristics used by the app:

- Service:
  - `0000fef1-0000-1000-8000-00805f9b34fb`
- Notify characteristics:
  - continuation notify: `c4edc000-9daf-11e3-8003-00025b000b00`
  - complete notify: `c4edc000-9daf-11e3-8004-00025b000b00`
- Write-only characteristics:
  - continuation write: `c4edc000-9daf-11e3-8003-10025b000b00`
  - complete write: `c4edc000-9daf-11e3-8004-10025b000b00`

App behavior:

- Requests MTU `81`
- Uses write-with-response (ATT `Write Request` observed in iPhone PacketLogger capture)
- Splits encrypted packet bytes into BLE chunks of `78`
  - all non-final chunks -> continuation write characteristic
  - final chunk -> complete write characteristic

## Mesh Packet Format

Encrypted mesh packet bytes (before BLE chunking):

1. `sequence` (3 bytes, little-endian, 24-bit)
2. `source_id` (2 bytes, little-endian)
3. `encrypted_payload` (variable)
4. `checksum` (8 bytes)
5. `ttl` (1 byte)

Decrypted payload (inside `encrypted_payload`) format:

1. `destination_id` (2 bytes, little-endian)
2. `packet_type` (1 byte)
3. `payload` (variable)

## Crypto

Derived from `CsrMeshCrypto` in the app:

- Network key derivation:
  - seed bytes: UTF-8 of `passphrase + "\0MCP"`
  - SHA-256 over seed
  - take last 16 digest bytes, reversed
- IV (`16` bytes):
  - `[seq0, seq1, seq2, 0x00, src0, src1, 0x00...0x00]` (10 trailing zero bytes)
- Payload encryption:
  - AES-128 OFB over decrypted payload
- Checksum:
  - HMAC-SHA256 over `8x00 + seq(3 LE) + src(2 LE) + encrypted_payload`
  - take last 8 HMAC bytes, reversed
- Default TTL observed in app sends: `5`

Default network passphrase constant in app:

- `6DeNmnsD5XUsf4UD`

Active runtime passphrase:

- Garden-specific `SmartGarden.password` value (from API user profile/gardens response).
- App switches mesh manager passphrase to active garden password on garden switch.

## Stream Transport (packet_type 112/113/114)

Transport is a start/data/end stream with ACK offsets.

- Start flush packet (`112`): payload `[0x00, 0x00]`
- Data packet (`113`): payload `[offset_le16] + data_chunk`
  - max chunk length `62`
- End flush packet (`112`): payload `[final_offset_le16]`
- ACK packet (`114`): payload `[ack_offset_le16]` or `[ack_offset_le16, 0xEF]`
  - final ACK may include trailing `0xEF` magic

Expected send flow:

1. send start flush -> expect ACK offset `0`
2. send data chunk(s) -> expect ACK offset = next byte offset
3. send end flush with final offset -> expect ACK offset = final length (with or without `0xEF`)

Timeout/retry behavior observed in app:

- per-segment ACK timeout: `600 ms`
- total TX stream timeout budget: `10200 ms`
- RX stream inactivity timeout: `15000 ms`

## Mesh Command Framing

Command payload for stream data is mesh command bytes:

- `cmd_type` (1 byte): request = `0x01`
- `opcode` (2 bytes, little-endian)
- `opcode payload` (variable)

## Line Control Opcodes

### 4103: Set Outlet/Line Mode

Opcode: `4103` (`0x1007`)

Payload format:

- `line_id` (1 byte)
- `mode_byte` (1 byte)
- `mode_mask` (1 byte)

Observed on/off use:

- ON: `mode_byte = 0x01`, `mode_mask = 0x01`
- OFF: `mode_byte = 0x00`, `mode_mask = 0x01`

### 4104: Set Outlet Brightness

Opcode constant `4104` exists in app constants.

- Runtime send path was not clearly referenced in current app flow.
- Current bridge/harness uses inferred payload `[line_id, brightness_0_255]`.
- This field mapping must be confirmed on hardware and adjusted if needed.

## Validation Vectors

Captured and used in harness selftest:

- Key derivation test (`passphrase="vitsch"`)
- HMAC-SHA256 test vector
- AES-OFB test vector
- full encrypted packet build/decrypt vector

These vectors are implemented in `tools/inlite_ble_harness.py` (`selftest` command).

## Known Unknowns

- Brightness opcode `4104` payload semantics beyond inferred `[line, value]`
- Scene/routine opcodes beyond line mode control in v1
- Potential hub-side constraints for high-frequency slider updates

## v1 Interop Contract (ESPHome External Component)

`inlite_hub:` options exposed:

- `ble_client_id`
- `esp32_ble_id`
- `hub_id`
- `network_passphrase_hex`
- `auto_discover`
- `discover_name_filter`
- `discover_match_address`
- `command_timeout`
- `retries`
- `poll_interval`

Entities exposed in v1:

- line lights (brightness-capable)
- BLE diagnostics:
  - RSSI sensor
  - connection-state binary sensor
  - last command status sensor
