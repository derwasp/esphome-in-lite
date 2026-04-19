#!/usr/bin/env python3
"""in-lite Smart Hub-150 BLE protocol harness.

This script mirrors the APK-discovered packet framing/crypto/stream behavior:
- Mesh packet encryption/checksum logic from CsrMeshCrypto
- Stream transport packet types 112/113/114
- BLE chunking (78 bytes) over continuation/final write-only characteristics

Use it to validate commands on real hardware before flashing ESPHome firmware.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Union

try:
    from bleak import BleakClient, BleakScanner
except Exception as exc:  # pragma: no cover - dependency guard
    print(
        "ERROR: bleak is required. Install with: pip install bleak pycryptodomex",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

try:
    from Cryptodome.Cipher import AES
except Exception as exc:  # pragma: no cover - dependency guard
    print(
        "ERROR: pycryptodomex is required. Install with: pip install pycryptodomex",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


UUID_MESH_SERVICE = "0000fef1-0000-1000-8000-00805f9b34fb"
UUID_CONTINUATION_CP = "c4edc000-9daf-11e3-8003-00025b000b00"
UUID_COMPLETE_CP = "c4edc000-9daf-11e3-8004-00025b000b00"
UUID_CONTINUATION_WR = "c4edc000-9daf-11e3-8003-10025b000b00"
UUID_COMPLETE_WR = "c4edc000-9daf-11e3-8004-10025b000b00"

PKT_TYPE_START_FLUSH = 112
PKT_TYPE_DATA = 113
PKT_TYPE_ACK = 114
PKT_TYPE_BLOCK_DATA = 115
# Protocol analysis shows packet type 116 behaves like RX stream-data.
PKT_TYPE_STREAM_DATA_ALT = 116
END_ACK_MAGIC = 0xEF
MAX_STREAM_CHUNK = 62
BLE_CHUNK = 78
DEFAULT_TTL = 5

CMD_TYPE_OOB = 0x03

OPCODE_SET_OUTLET_MODE = 4103
OPCODE_GET_INFO_DEVICES = 5
OPCODE_OOB_OUTLET_MODE_UPDATE = 24
OPCODE_OOB_ALL_OUTLETS_MODE_UPDATE = 33
PRODUCT_TYPE_SMART_HUB = 3


@dataclass
class DecryptedPacket:
    sequence: int
    source_id: int
    destination_id: int
    packet_type: int
    ttl: int
    payload: bytes


@dataclass(frozen=True)
class LineModeState:
    line_id: int
    output_mode: int
    output_state: int
    output_rtc_timer: int

    @property
    def on(self) -> bool:
        return (self.output_mode & 0x01) != 0


@dataclass
class RxStreamState:
    source_id: int
    destination_id: int
    payload: bytearray = field(default_factory=bytearray)
    acked_bytes: int = 0
    completed: bool = False


def parse_block_line_mode_updates(payload: bytes) -> list[LineModeState]:
    if len(payload) < 3:
        return []

    cmd_type = payload[0]
    opcode = payload[1] | (payload[2] << 8)
    if cmd_type != CMD_TYPE_OOB:
        return []

    if opcode == OPCODE_OOB_OUTLET_MODE_UPDATE:
        if len(payload) < 6:
            return []
        rtc = payload[6] if len(payload) >= 7 else 0
        return [
            LineModeState(
                line_id=payload[3],
                output_mode=payload[4],
                output_state=payload[5],
                output_rtc_timer=rtc,
            )
        ]

    if opcode != OPCODE_OOB_ALL_OUTLETS_MODE_UPDATE:
        return []

    body = payload[3:]
    if len(body) < 4:
        return []

    usable_len = len(body) - (len(body) % 4)
    out: list[LineModeState] = []
    for i in range(0, usable_len, 4):
        out.append(
            LineModeState(
                line_id=body[i],
                output_mode=body[i + 1],
                output_state=body[i + 2],
                output_rtc_timer=body[i + 3],
            )
        )
    return out


def parse_get_info_devices_line_modes(payload: bytes) -> list[LineModeState]:
    """Parse Smart Hub line states from a GET_INFO_DEVICES response.

    Payload analysis shows that for Smart Hub product type 3, byte 7 is the
    outlet count and the per-outlet records start at byte 8. Two layouts are
    used:
    - compact form: 7 bytes per outlet
    - extended form: 23 bytes per outlet (name/icon included)
    """
    if len(payload) < 8:
        return []

    opcode = payload[1] | (payload[2] << 8)
    if opcode != OPCODE_GET_INFO_DEVICES:
        return []

    if payload[4] != PRODUCT_TYPE_SMART_HUB:
        return []

    outlet_count = payload[7]
    if outlet_count <= 0:
        return []

    compact_len = 8 + (outlet_count * 7)
    extended_len = 8 + (outlet_count * 23)
    if len(payload) >= extended_len:
        outlet_size = 23
    elif len(payload) >= compact_len:
        outlet_size = 7
    else:
        return []

    out: list[LineModeState] = []
    pos = 8
    for _ in range(outlet_count):
        chunk = payload[pos : pos + outlet_size]
        if len(chunk) < 7:
            return []
        out.append(
            LineModeState(
                line_id=chunk[0],
                output_mode=chunk[3],
                output_state=chunk[6],
                output_rtc_timer=0,
            )
        )
        pos += outlet_size

    out.sort(key=lambda state: state.line_id)
    return out


class InliteCrypto:
    def __init__(
        self,
        passphrase: Union[str, bytes],
        controller_id: int,
        sequence_seed: Optional[int] = None,
    ) -> None:
        self.passphrase = passphrase
        self.key = self.get_encrypted_key(passphrase)
        self.controller_id = controller_id
        self.sequence = (
            sequence_seed
            if sequence_seed is not None
            else random.randint(0, 0xFFFFFF)
        )

    @staticmethod
    def get_encrypted_key(passphrase: Union[str, bytes]) -> bytes:
        if isinstance(passphrase, bytes):
            passphrase_bytes = passphrase
        else:
            passphrase_bytes = passphrase.encode("utf-8")
        digest = hashlib.sha256(passphrase_bytes + b"\0MCP").digest()
        return bytes(digest[-1 - i] for i in range(16))

    @staticmethod
    def _iv(sequence: int, source_id: int) -> bytes:
        return bytes(
            [
                sequence & 0xFF,
                (sequence >> 8) & 0xFF,
                (sequence >> 16) & 0xFF,
                0x00,
                source_id & 0xFF,
                (source_id >> 8) & 0xFF,
            ]
            + [0x00] * 10
        )

    @staticmethod
    def _aes_ofb(data: bytes, key: bytes, iv: bytes) -> bytes:
        return AES.new(key, AES.MODE_OFB, iv=iv).encrypt(data)

    @staticmethod
    def _checksum(sequence: int, source_id: int, encrypted_payload: bytes, key: bytes) -> bytes:
        base = bytes(8) + bytes(
            [
                sequence & 0xFF,
                (sequence >> 8) & 0xFF,
                (sequence >> 16) & 0xFF,
                source_id & 0xFF,
                (source_id >> 8) & 0xFF,
            ]
        ) + encrypted_payload
        digest = hmac.new(key, base, hashlib.sha256).digest()
        return bytes(digest[-1 - i] for i in range(8))

    def build_encrypted_packet(
        self,
        destination_id: int,
        packet_type: int,
        payload: bytes,
        ttl: int = DEFAULT_TTL,
    ) -> bytes:
        self.sequence = (self.sequence + 1) & 0xFFFFFF
        plain = bytes(
            [
                destination_id & 0xFF,
                (destination_id >> 8) & 0xFF,
                packet_type & 0xFF,
            ]
        ) + payload
        encrypted_payload = self._aes_ofb(
            plain, self.key, self._iv(self.sequence, self.controller_id)
        )
        checksum = self._checksum(self.sequence, self.controller_id, encrypted_payload, self.key)

        return (
            bytes(
                [
                    self.sequence & 0xFF,
                    (self.sequence >> 8) & 0xFF,
                    (self.sequence >> 16) & 0xFF,
                    self.controller_id & 0xFF,
                    (self.controller_id >> 8) & 0xFF,
                ]
            )
            + encrypted_payload
            + checksum
            + bytes([ttl & 0xFF])
        )

    def decrypt_packet(self, encrypted_packet: bytes) -> Optional[DecryptedPacket]:
        if len(encrypted_packet) < 14:
            return None

        sequence = (
            encrypted_packet[0]
            | (encrypted_packet[1] << 8)
            | (encrypted_packet[2] << 16)
        )
        source_id = encrypted_packet[3] | (encrypted_packet[4] << 8)
        ttl = encrypted_packet[-1]

        checksum = encrypted_packet[-9:-1]
        encrypted_payload = encrypted_packet[5:-9]
        expected_checksum = self._checksum(sequence, source_id, encrypted_payload, self.key)
        if checksum != expected_checksum:
            return None

        plain = self._aes_ofb(encrypted_payload, self.key, self._iv(sequence, source_id))
        if len(plain) < 3:
            return None

        destination_id = plain[0] | (plain[1] << 8)
        packet_type = plain[2]
        payload = plain[3:]

        return DecryptedPacket(
            sequence=sequence,
            source_id=source_id,
            destination_id=destination_id,
            packet_type=packet_type,
            ttl=ttl,
            payload=payload,
        )


class InliteBleHarness:
    def __init__(
        self,
        mac: str,
        hub_id: int,
        crypto: InliteCrypto,
        timeout_ms: int,
        retries: int,
        write_with_response: bool = True,
        verbose: bool = False,
    ) -> None:
        self.mac = mac
        self.hub_id = hub_id
        self.crypto = crypto
        self.timeout_s = timeout_ms / 1000.0
        self.retries = retries
        self.write_with_response = write_with_response
        self.verbose = verbose

        self.client = BleakClient(mac)
        self._incoming = bytearray()
        self._ack_queue: asyncio.Queue[tuple[int, bool]] = asyncio.Queue()
        self._line_mode_queue: asyncio.Queue[list[LineModeState]] = asyncio.Queue()
        self._line_modes: dict[int, LineModeState] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rx_packet_queue: asyncio.Queue[DecryptedPacket] = asyncio.Queue()
        self._rx_worker_task: Optional[asyncio.Task[None]] = None
        self._rx_streams: dict[int, RxStreamState] = {}
        self._rx_completed_flush_offsets: dict[int, int] = {}
        self._write_lock = asyncio.Lock()

    async def __aenter__(self) -> "InliteBleHarness":
        self._loop = asyncio.get_running_loop()
        notify_started: list[str] = []
        try:
            await self.client.connect()
            await self.client.start_notify(UUID_CONTINUATION_CP, self._on_continuation)
            notify_started.append(UUID_CONTINUATION_CP)
            await self.client.start_notify(UUID_COMPLETE_CP, self._on_complete)
            notify_started.append(UUID_COMPLETE_CP)
            self._rx_worker_task = asyncio.create_task(self._rx_worker())
            return self
        except Exception:
            for char_uuid in reversed(notify_started):
                try:
                    await self.client.stop_notify(char_uuid)
                except Exception:
                    pass
            self._loop = None
            self._rx_streams.clear()
            self._rx_completed_flush_offsets.clear()
            try:
                await self.client.disconnect()
            except Exception:
                pass
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self.client.stop_notify(UUID_CONTINUATION_CP)
            await self.client.stop_notify(UUID_COMPLETE_CP)
        except Exception:
            pass
        if self._rx_worker_task is not None:
            self._rx_worker_task.cancel()
            try:
                await self._rx_worker_task
            except asyncio.CancelledError:
                pass
            self._rx_worker_task = None
        self._loop = None
        self._rx_streams.clear()
        self._rx_completed_flush_offsets.clear()
        await self.client.disconnect()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def _publish_line_mode_updates(self, updates: list[LineModeState], *, source: str) -> None:
        if not updates:
            return
        for update in updates:
            self._line_modes[update.line_id] = update
        self._line_mode_queue.put_nowait(updates)
        self._log(
            f"[state] source={source} "
            + ", ".join(
                f"line={u.line_id} on={str(u.on).lower()} mode=0x{u.output_mode:02x} "
                f"state=0x{u.output_state:02x} rtc={u.output_rtc_timer}"
                for u in updates
            )
        )

    def _on_continuation(self, _handle, data: bytearray) -> None:
        self._incoming.extend(data)

    def _on_complete(self, _handle, data: bytearray) -> None:
        self._incoming.extend(data)
        packet_bytes = bytes(self._incoming)
        self._incoming.clear()

        decrypted = self.crypto.decrypt_packet(packet_bytes)
        if decrypted is None:
            self._log("[rx] dropped packet (checksum/decrypt failed)")
            return

        self._log(
            f"[rx] src=0x{decrypted.source_id:04x} dst=0x{decrypted.destination_id:04x} "
            f"type={decrypted.packet_type} payload={decrypted.payload.hex()}"
        )
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._rx_packet_queue.put_nowait, decrypted)
        except RuntimeError:
            # Event loop is already shutting down.
            return

    async def _rx_worker(self) -> None:
        while True:
            packet = await self._rx_packet_queue.get()
            try:
                await self._handle_incoming_packet(packet)
            except Exception as exc:
                self._log(f"[rx] incoming handler failed: {exc}")

    async def _handle_incoming_packet(self, decrypted: DecryptedPacket) -> None:
        if (
            decrypted.packet_type == PKT_TYPE_ACK
            and decrypted.source_id == self.hub_id
            and len(decrypted.payload) >= 2
        ):
            offset = decrypted.payload[0] | (decrypted.payload[1] << 8)
            end_ack = len(decrypted.payload) >= 3 and decrypted.payload[2] == END_ACK_MAGIC
            self._ack_queue.put_nowait((offset, end_ack))
            return

        if (
            decrypted.packet_type == PKT_TYPE_START_FLUSH
            and decrypted.source_id == self.hub_id
            and len(decrypted.payload) >= 2
        ):
            await self._handle_rx_stream_flush(decrypted)
            return

        if (
            decrypted.packet_type in {PKT_TYPE_DATA, PKT_TYPE_STREAM_DATA_ALT}
            and decrypted.source_id == self.hub_id
            and len(decrypted.payload) >= 2
        ):
            await self._handle_rx_stream_data(decrypted)
            return

        if (
            decrypted.packet_type == PKT_TYPE_BLOCK_DATA
            and decrypted.source_id == self.hub_id
        ):
            updates = parse_block_line_mode_updates(decrypted.payload)
            if not updates:
                return
            self._publish_line_mode_updates(updates, source="oob")

    async def _send_stream_ack_packet(
        self,
        destination_id: int,
        offset: int,
        *,
        end_ack: bool,
    ) -> None:
        payload = bytes([offset & 0xFF, (offset >> 8) & 0xFF])
        if end_ack:
            payload += bytes([END_ACK_MAGIC])
        packet = self.crypto.build_encrypted_packet(destination_id, PKT_TYPE_ACK, payload)
        await self._send_encrypted_packet(packet)
        ack_kind = "END_ACK" if end_ack else "ACK"
        self._log(
            f"[ack] sent reverse-stream {ack_kind} offset=0x{offset:04x} "
            f"to 0x{destination_id:04x}"
        )

    async def _handle_rx_stream_flush(self, decrypted: DecryptedPacket) -> None:
        offset = decrypted.payload[0] | (decrypted.payload[1] << 8)
        stream = self._rx_streams.get(decrypted.source_id)

        if offset == 0:
            self._rx_completed_flush_offsets.pop(decrypted.source_id, None)
            self._rx_streams[decrypted.source_id] = RxStreamState(
                source_id=decrypted.source_id,
                destination_id=decrypted.destination_id,
            )
            self._log(
                f"[rx] reverse-stream START_FLUSH from 0x{decrypted.source_id:04x}; "
                "acknowledging offset=0"
            )
            await self._send_stream_ack_packet(decrypted.source_id, 0, end_ack=False)
            return

        if stream is None:
            completed_offset = self._rx_completed_flush_offsets.get(decrypted.source_id)
            if completed_offset == offset:
                self._log(
                    f"[ack] duplicate reverse-stream END_FLUSH from 0x{decrypted.source_id:04x}; "
                    f"re-sending END_ACK offset=0x{offset:04x}"
                )
                await self._send_stream_ack_packet(
                    decrypted.source_id, offset, end_ack=True
                )
                return
            self._log(
                f"[ack] no active reverse stream for 0x{decrypted.source_id:04x}; "
                f"ignoring END_FLUSH offset=0x{offset:04x}"
            )
            return

        if offset == stream.acked_bytes:
            preview = bytes(stream.payload[:32]).hex()
            if len(stream.payload) > 32:
                preview += "..."
            self._log(
                f"[rx] reverse-stream complete from 0x{decrypted.source_id:04x} "
                f"len={stream.acked_bytes} preview={preview}"
            )
            stream.completed = True
            await self._send_stream_ack_packet(
                decrypted.source_id, stream.acked_bytes, end_ack=True
            )
            bootstrap_updates = parse_get_info_devices_line_modes(bytes(stream.payload))
            if bootstrap_updates:
                self._publish_line_mode_updates(
                    bootstrap_updates, source="get_info_devices"
                )
            self._rx_completed_flush_offsets[decrypted.source_id] = stream.acked_bytes
            self._rx_streams.pop(decrypted.source_id, None)
            return

        self._log(
            f"[ack] reverse-stream END_FLUSH mismatch from 0x{decrypted.source_id:04x}; "
            f"got=0x{offset:04x} current=0x{stream.acked_bytes:04x}, re-acking current"
        )
        await self._send_stream_ack_packet(
            decrypted.source_id, stream.acked_bytes, end_ack=False
        )

    async def _handle_rx_stream_data(self, decrypted: DecryptedPacket) -> None:
        offset = decrypted.payload[0] | (decrypted.payload[1] << 8)
        chunk = decrypted.payload[2:]
        stream = self._rx_streams.get(decrypted.source_id)
        if stream is None or stream.completed:
            self._log(
                f"[ack] no active reverse stream for 0x{decrypted.source_id:04x}; "
                f"ignoring DATA offset=0x{offset:04x}"
            )
            return

        if offset != stream.acked_bytes:
            self._log(
                f"[ack] reverse-stream DATA out of order from 0x{decrypted.source_id:04x}; "
                f"expected=0x{stream.acked_bytes:04x} got=0x{offset:04x}, re-acking current"
            )
            await self._send_stream_ack_packet(
                decrypted.source_id, stream.acked_bytes, end_ack=False
            )
            return

        stream.payload.extend(chunk)
        stream.acked_bytes += len(chunk)
        self._log(
            f"[rx] reverse-stream DATA from 0x{decrypted.source_id:04x} "
            f"offset=0x{offset:04x} len={len(chunk)} acked=0x{stream.acked_bytes:04x}"
        )
        await self._send_stream_ack_packet(
            decrypted.source_id, stream.acked_bytes, end_ack=False
        )

    async def _send_encrypted_packet(self, packet: bytes) -> None:
        async with self._write_lock:
            for i in range(0, len(packet), BLE_CHUNK):
                chunk = packet[i : i + BLE_CHUNK]
                is_last = i + BLE_CHUNK >= len(packet)
                char_uuid = UUID_COMPLETE_WR if is_last else UUID_CONTINUATION_WR
                await self.client.write_gatt_char(
                    char_uuid,
                    chunk,
                    response=self.write_with_response,
                )

    async def _await_ack(self, expected_offset: int, end_phase: bool) -> bool:
        while True:
            try:
                offset, end_ack = await asyncio.wait_for(
                    self._ack_queue.get(), timeout=self.timeout_s
                )
            except asyncio.TimeoutError:
                return False

            if offset != expected_offset:
                self._log(
                    f"[ack] ignoring offset=0x{offset:04x}, expected=0x{expected_offset:04x}"
                )
                continue

            if end_phase:
                return True  # both regular final ACK and END_ACK are accepted

            if end_ack:
                self._log("[ack] ignoring END_ACK during non-end phase")
                continue

            return True

    def get_line_modes_snapshot(self) -> dict[int, LineModeState]:
        return dict(self._line_modes)

    async def collect_line_modes(self, duration_s: float) -> dict[int, LineModeState]:
        duration = max(0.0, duration_s)
        deadline = asyncio.get_running_loop().time() + duration
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._line_mode_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        return self.get_line_modes_snapshot()

    async def send_stream(self, payload: bytes) -> None:
        # START_FLUSH (offset 0)
        for attempt in range(self.retries + 1):
            pkt = self.crypto.build_encrypted_packet(
                self.hub_id, PKT_TYPE_START_FLUSH, bytes([0x00, 0x00])
            )
            await self._send_encrypted_packet(pkt)
            self._log(f"[tx] START_FLUSH attempt={attempt}")
            if await self._await_ack(0, end_phase=False):
                break
            if attempt == self.retries:
                raise TimeoutError("START_FLUSH ACK timeout")

        # DATA chunks
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + MAX_STREAM_CHUNK]
            next_offset = offset + len(chunk)
            chunk_payload = bytes([offset & 0xFF, (offset >> 8) & 0xFF]) + chunk

            acked = False
            for attempt in range(self.retries + 1):
                pkt = self.crypto.build_encrypted_packet(
                    self.hub_id, PKT_TYPE_DATA, chunk_payload
                )
                await self._send_encrypted_packet(pkt)
                self._log(
                    f"[tx] DATA offset=0x{offset:04x} len={len(chunk)} attempt={attempt}"
                )
                if await self._await_ack(next_offset, end_phase=False):
                    acked = True
                    break
                if attempt == self.retries:
                    raise TimeoutError(f"DATA ACK timeout for offset 0x{next_offset:04x}")

            if not acked:
                raise TimeoutError("DATA ACK timeout")
            offset = next_offset

        # END_FLUSH (offset = total bytes)
        final_offset = len(payload)
        end_payload = bytes([final_offset & 0xFF, (final_offset >> 8) & 0xFF])
        for attempt in range(self.retries + 1):
            pkt = self.crypto.build_encrypted_packet(
                self.hub_id, PKT_TYPE_START_FLUSH, end_payload
            )
            await self._send_encrypted_packet(pkt)
            self._log(f"[tx] END_FLUSH offset=0x{final_offset:04x} attempt={attempt}")
            if await self._await_ack(final_offset, end_phase=True):
                return
            if attempt == self.retries:
                raise TimeoutError("END_FLUSH ACK timeout")


def mesh_command(opcode: int, data: bytes) -> bytes:
    return bytes([0x01, opcode & 0xFF, (opcode >> 8) & 0xFF]) + data


def cmd_set_outlet_mode(line_id: int, on: bool) -> bytes:
    mode_byte = 0x01 if on else 0x00
    mode_mask = 0x01
    return mesh_command(OPCODE_SET_OUTLET_MODE, bytes([line_id & 0xFF, mode_byte, mode_mask]))


def cmd_get_info_devices() -> bytes:
    return mesh_command(OPCODE_GET_INFO_DEVICES, b"")


def is_mac_address(value: str) -> bool:
    return re.fullmatch(r"(?i)[0-9a-f]{2}(?::[0-9a-f]{2}){5}", value.strip()) is not None


def parse_passphrase_hex(value: str) -> bytes:
    cleaned = value.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) == 0:
        raise ValueError("passphrase hex is empty")
    if len(cleaned) % 2 != 0:
        raise ValueError("passphrase hex must have an even number of characters")
    if re.fullmatch(r"[0-9a-f]+", cleaned) is None:
        raise ValueError("passphrase hex must contain only 0-9, a-f")
    return bytes.fromhex(cleaned)


def resolve_passphrase(passphrase_hex: Optional[str]) -> bytes:
    if not passphrase_hex:
        raise ValueError("provide --passphrase-hex")
    return parse_passphrase_hex(passphrase_hex)


def _fmt_services(services: list[str]) -> str:
    if not services:
        return "-"
    return ",".join(services)


def _scan_sort_key(row: dict) -> tuple[bool, bool, bool, int]:
    return (
        row["match_hit"],
        row["service_hit"],
        row["name_hit"],
        -999 if row["rssi"] is None else row["rssi"],
    )


async def discover_candidates(
    seconds: float,
    name_filter: str,
    match_address: Optional[str] = None,
    include_all: bool = False,
) -> list[dict]:
    try:
        discovered = await BleakScanner.discover(timeout=seconds, return_adv=True)
    except TypeError:
        # Backward compatibility for older bleak that doesn't support return_adv.
        discovered = await BleakScanner.discover(timeout=seconds)

    rows: list[dict] = []
    if isinstance(discovered, dict):
        entries = discovered.values()
    else:
        entries = discovered

    for entry in entries:
        if isinstance(entry, tuple) and len(entry) == 2:
            device, adv = entry
        else:
            device = entry
            adv = None

        address = getattr(device, "address", "") or ""
        dev_name = getattr(device, "name", "") or ""
        adv_name = (getattr(adv, "local_name", "") or "") if adv is not None else ""
        name = adv_name or dev_name

        services = []
        if adv is not None:
            raw_services = getattr(adv, "service_uuids", None) or []
            services = [str(s).lower() for s in raw_services]

        rssi = None
        if adv is not None:
            rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)

        name_hit = name_filter.lower() in name.lower() if name else False
        service_hit = UUID_MESH_SERVICE in services

        match_hit = False
        if match_address:
            match_hit = address.lower() == match_address.lower()

        if not include_all and not (name_hit or service_hit or match_hit):
            continue

        rows.append(
            {
                "address": address,
                "name": name or "-",
                "rssi": rssi,
                "services": services,
                "name_hit": name_hit,
                "service_hit": service_hit,
                "match_hit": match_hit,
            }
        )

    rows.sort(key=_scan_sort_key, reverse=True)
    return rows


async def run_scan(args: argparse.Namespace) -> int:
    print(
        f"scanning {args.seconds:.1f}s for BLE devices (name~{args.name_filter!r}, service={UUID_MESH_SERVICE})"
    )

    try:
        rows = await discover_candidates(
            seconds=args.seconds,
            name_filter=args.name_filter,
            match_address=args.match_address,
            include_all=args.all,
        )
    except Exception as exc:
        print(f"ERROR: BLE scan failed: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("no matching devices found")
        print(
            "tip: keep hub powered, close the in-lite app on iPhone, and retry with a longer scan."
        )
        return 1

    for row in rows:
        rssi = "?" if row["rssi"] is None else str(row["rssi"])
        print(
            f"candidate address={row['address']} name={row['name']} rssi={rssi} "
            f"name_hit={str(row['name_hit']).lower()} service_hit={str(row['service_hit']).lower()} "
            f"services={_fmt_services(row['services'])}"
        )

    best = rows[0]
    print(f"best_address={best['address']}")
    if sys.platform == "darwin" and is_mac_address(best["address"]):
        print(
            "note: if this value later appears as a UUID in macOS scans, use that UUID for --mac."
        )
    elif sys.platform == "darwin" and not is_mac_address(best["address"]):
        print(
            "note: macOS uses CoreBluetooth UUID addresses; pass this UUID as --mac."
        )

    return 0


def run_selftest() -> int:
    key = InliteCrypto.get_encrypted_key("vitsch")
    expected_key = bytes([45, 32, 212, 107, 106, 99, 87, 214, 185, 131, 204, 177, 56, 119, 215, 16])
    if key != expected_key:
        print("selftest failed: key derivation mismatch", file=sys.stderr)
        return 1

    hmac_test = hmac.new(
        b"1234567890OPJKLM", b"BlaBlaBlaBlaBlaBlaBlaFooBar", hashlib.sha256
    ).digest()
    expected_hmac = bytes(
        [
            19,
            76,
            71,
            2,
            186,
            140,
            184,
            194,
            97,
            213,
            33,
            73,
            208,
            252,
            180,
            218,
            155,
            228,
            226,
            183,
            14,
            3,
            40,
            76,
            171,
            250,
            141,
            141,
            128,
            20,
            144,
            51,
        ]
    )
    if hmac_test != expected_hmac:
        print("selftest failed: HMAC mismatch", file=sys.stderr)
        return 1

    crypt = AES.new(b"Abcdefghijklmnop", AES.MODE_OFB, iv=b"Hijklmnopqrstuvw").encrypt(
        b"Test Foo Bar"
    )
    if crypt != bytes([190, 123, 206, 219, 121, 222, 223, 165, 17, 3, 39, 192]):
        print("selftest failed: AES OFB encrypt mismatch", file=sys.stderr)
        return 1

    crypto = InliteCrypto("vitsch", controller_id=43981, sequence_seed=1193045)
    pkt = crypto.build_encrypted_packet(17767, 72, b"ello World!", ttl=10)
    expected_pkt = bytes(
        [
            86,
            52,
            18,
            205,
            171,
            205,
            239,
            94,
            156,
            181,
            200,
            90,
            106,
            112,
            248,
            35,
            97,
            150,
            68,
            3,
            155,
            5,
            142,
            139,
            141,
            202,
            42,
            10,
        ]
    )
    if pkt != expected_pkt:
        print("selftest failed: encrypted packet mismatch", file=sys.stderr)
        return 1

    parsed = crypto.decrypt_packet(expected_pkt)
    if not parsed:
        print("selftest failed: decrypt returned None", file=sys.stderr)
        return 1
    if parsed.sequence != 1193046 or parsed.source_id != 43981 or parsed.ttl != 10:
        print("selftest failed: decrypted metadata mismatch", file=sys.stderr)
        return 1
    if parsed.payload != b"ello World!":
        print("selftest failed: decrypted payload mismatch", file=sys.stderr)
        return 1

    sample_oob_all = bytes.fromhex("032100000000000101010002000000")
    parsed_all = parse_block_line_mode_updates(sample_oob_all)
    if len(parsed_all) != 3:
        print("selftest failed: OOB all-lines parser count mismatch", file=sys.stderr)
        return 1
    if parsed_all[1].line_id != 1 or not parsed_all[1].on:
        print("selftest failed: OOB all-lines parser value mismatch", file=sys.stderr)
        return 1

    sample_oob_single = bytes.fromhex("03180002010100")
    parsed_single = parse_block_line_mode_updates(sample_oob_single)
    if len(parsed_single) != 1 or parsed_single[0].line_id != 2 or not parsed_single[0].on:
        print("selftest failed: OOB single-line parser mismatch", file=sys.stderr)
        return 1

    sample_get_info_devices = bytes.fromhex(
        "0205000c03340003"
        "00010100000000"
        "01010101000001"
        "02010100000000"
    )
    parsed_device_info = parse_get_info_devices_line_modes(sample_get_info_devices)
    if len(parsed_device_info) != 3:
        print("selftest failed: GET_INFO_DEVICES parser count mismatch", file=sys.stderr)
        return 1
    if parsed_device_info[0].line_id != 0 or parsed_device_info[0].on:
        print("selftest failed: GET_INFO_DEVICES line 0 mismatch", file=sys.stderr)
        return 1
    if parsed_device_info[1].line_id != 1 or not parsed_device_info[1].on:
        print("selftest failed: GET_INFO_DEVICES line 1 mismatch", file=sys.stderr)
        return 1

    print("selftest passed")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="in-lite Smart Hub-150 BLE command harness"
    )
    parser.add_argument(
        "--mac",
        help="Smart Hub BLE address (MAC or CoreBluetooth UUID on macOS)",
    )
    parser.add_argument("--hub-id", type=lambda x: int(x, 0), help="Smart Hub mesh ID (for example 0x1234)")
    parser.add_argument(
        "--passphrase-hex",
        default=None,
        help="Network passphrase raw bytes as hex (required, for example c3b9aa001122...)",
    )
    parser.add_argument(
        "--controller-id",
        type=lambda x: int(x, 0),
        default=None,
        help="Override controller mesh ID (default: random 32768..65533)",
    )
    parser.add_argument("--timeout-ms", type=int, default=600)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--write-with-response",
        dest="write_with_response",
        action="store_true",
        default=True,
        help="Use GATT write-with-response (default, matches app trace)",
    )
    parser.add_argument(
        "--write-no-response",
        dest="write_with_response",
        action="store_false",
        help="Use GATT write-without-response",
    )
    parser.add_argument("--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="Run protocol crypto/vector self-tests")
    scan = sub.add_parser(
        "scan",
        help="Scan BLE and print candidate in-lite hubs (useful on macOS to get CoreBluetooth UUID)",
    )
    scan.add_argument("--seconds", type=float, default=12.0, help="Scan duration in seconds")
    scan.add_argument(
        "--name-filter",
        default="inlite",
        help="Case-insensitive name substring filter (default: inlite)",
    )
    scan.add_argument(
        "--match-address",
        default=None,
        help="Highlight exact address if seen (MAC or UUID)",
    )
    scan.add_argument("--all", action="store_true", help="Show all devices from scan")

    line = sub.add_parser("line", help="Set line on/off")
    line.add_argument("line", type=int, help="Line number (0..15)")
    line.add_argument("state", choices=["on", "off"])
    line.add_argument(
        "--auto-discover",
        action="store_true",
        help="Auto-discover hub address before sending command (also implied when --mac is omitted)",
    )
    line.add_argument(
        "--discover-seconds",
        type=float,
        default=12.0,
        help="Discovery scan duration for line command autodiscovery",
    )
    line.add_argument(
        "--discover-name-filter",
        default="inlite",
        help="Discovery name substring filter for line command autodiscovery",
    )
    line.add_argument(
        "--discover-match-address",
        default=None,
        help="Preferred address during autodiscovery (MAC or CoreBluetooth UUID)",
    )

    query = sub.add_parser(
        "query",
        help="Listen for line-state updates (OOB opcodes 24/33) and print current states",
    )
    query.add_argument(
        "--auto-discover",
        action="store_true",
        help="Auto-discover hub address before querying (also implied when --mac is omitted)",
    )
    query.add_argument(
        "--discover-seconds",
        type=float,
        default=12.0,
        help="Discovery scan duration for query autodiscovery",
    )
    query.add_argument(
        "--discover-name-filter",
        default="inlite",
        help="Discovery name substring filter for query autodiscovery",
    )
    query.add_argument(
        "--discover-match-address",
        default=None,
        help="Preferred address during autodiscovery (MAC or CoreBluetooth UUID)",
    )
    query.add_argument(
        "--listen-seconds",
        type=float,
        default=6.0,
        help="Seconds to listen for line updates after connect/trigger (default: 6.0)",
    )
    query.add_argument(
        "--trigger-get-info",
        action="store_true",
        help="Send opcode 5 GET_INFO_DEVICES before listening (experimental)",
    )
    query.add_argument(
        "--line",
        type=int,
        default=None,
        help="Optional line id filter (0..15)",
    )
    query.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of text lines",
    )
    query.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit 0 even when no line-state updates were received",
    )

    return parser.parse_args()


async def resolve_target_mac(
    args: argparse.Namespace, status_stream=sys.stdout
) -> tuple[str | None, int]:
    target_mac = args.mac

    if args.auto_discover or not target_mac:
        match_address = args.discover_match_address or target_mac
        print(
            f"autodiscovery scan {args.discover_seconds:.1f}s (name~{args.discover_name_filter!r}, service={UUID_MESH_SERVICE})",
            file=status_stream,
        )
        try:
            rows = await discover_candidates(
                seconds=args.discover_seconds,
                name_filter=args.discover_name_filter,
                match_address=match_address,
            )
        except Exception as exc:
            print(f"ERROR: autodiscovery failed: {exc}", file=sys.stderr)
            return None, 1

        if not rows:
            print("ERROR: autodiscovery found no matching hub", file=sys.stderr)
            print(
                "tip: keep hub powered, close the in-lite app on iPhone, and retry with a longer scan.",
                file=sys.stderr,
            )
            return None, 1

        best = rows[0]
        target_mac = best["address"]
        rssi = "?" if best["rssi"] is None else str(best["rssi"])
        print(
            f"autodiscovery selected address={best['address']} name={best['name']} rssi={rssi} "
            f"name_hit={str(best['name_hit']).lower()} service_hit={str(best['service_hit']).lower()}",
            file=status_stream,
        )

    if not target_mac:
        print("ERROR: --mac is required when autodiscovery is disabled", file=sys.stderr)
        return None, 2

    if sys.platform == "darwin" and is_mac_address(target_mac):
        print(
            "INFO: On macOS, Bleak may require a CoreBluetooth UUID instead of BLE MAC.",
            file=sys.stderr,
        )
        print(
            "INFO: Run `... inlite_ble_harness.py scan --seconds 12 --name-filter inlite` or use command --auto-discover.",
            file=sys.stderr,
        )

    return target_mac, 0


async def run_line(args: argparse.Namespace) -> int:
    if args.hub_id is None:
        print("ERROR: --hub-id is required for line commands", file=sys.stderr)
        return 2
    if args.line < 0 or args.line > 15:
        print("ERROR: line must be in range 0..15", file=sys.stderr)
        return 2

    try:
        passphrase_value = resolve_passphrase(args.passphrase_hex)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    target_mac, rc = await resolve_target_mac(args)
    if rc != 0 or target_mac is None:
        return rc

    if args.controller_id is None:
        controller_id = random.randint(32768, 65533)
    else:
        controller_id = args.controller_id

    crypto = InliteCrypto(passphrase_value, controller_id=controller_id)

    print(
        f"connecting mac={target_mac} hub_id=0x{args.hub_id:04x} controller_id=0x{controller_id:04x}"
    )
    print(f"write_with_response={args.write_with_response}")

    payloads: list[bytes] = [cmd_set_outlet_mode(args.line, args.state == "on")]

    try:
        async with InliteBleHarness(
            mac=target_mac,
            hub_id=args.hub_id,
            crypto=crypto,
            timeout_ms=args.timeout_ms,
            retries=args.retries,
            write_with_response=args.write_with_response,
            verbose=args.verbose,
        ) as h:
            for p in payloads:
                await h.send_stream(p)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if sys.platform == "darwin" and "was not found" in str(exc):
            print(
                "HINT: Use scan mode to discover the current CoreBluetooth UUID and pass it in --mac.",
                file=sys.stderr,
            )
        return 1

    print("command(s) sent successfully")
    return 0


def format_line_mode_state(state: LineModeState) -> str:
    return (
        f"line={state.line_id} on={str(state.on).lower()} "
        f"output_mode=0x{state.output_mode:02x} output_state=0x{state.output_state:02x} "
        f"output_rtc_timer={state.output_rtc_timer}"
    )


async def run_query(args: argparse.Namespace) -> int:
    if args.hub_id is None:
        print("ERROR: --hub-id is required for query commands", file=sys.stderr)
        return 2
    if args.line is not None and (args.line < 0 or args.line > 15):
        print("ERROR: --line must be in range 0..15", file=sys.stderr)
        return 2

    try:
        passphrase_value = resolve_passphrase(args.passphrase_hex)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    status_stream = sys.stderr if args.json else sys.stdout

    target_mac, rc = await resolve_target_mac(args, status_stream=status_stream)
    if rc != 0 or target_mac is None:
        return rc

    if args.controller_id is None:
        controller_id = random.randint(32768, 65533)
    else:
        controller_id = args.controller_id

    crypto = InliteCrypto(passphrase_value, controller_id=controller_id)

    if args.verbose and args.json:
        print(
            "INFO: --json enabled; suppressing verbose packet logs to keep stdout valid JSON.",
            file=sys.stderr,
        )

    print(
        f"connecting mac={target_mac} hub_id=0x{args.hub_id:04x} controller_id=0x{controller_id:04x}",
        file=status_stream,
    )
    print(f"write_with_response={args.write_with_response}", file=status_stream)

    try:
        async with InliteBleHarness(
            mac=target_mac,
            hub_id=args.hub_id,
            crypto=crypto,
            timeout_ms=args.timeout_ms,
            retries=args.retries,
            write_with_response=args.write_with_response,
            verbose=args.verbose and not args.json,
        ) as h:
            if args.trigger_get_info:
                print("sending GET_INFO_DEVICES (opcode 5) trigger", file=status_stream)
                await h.send_stream(cmd_get_info_devices())
            print(f"listening for line updates for {args.listen_seconds:.1f}s", file=status_stream)
            states = h.get_line_modes_snapshot()
            if args.listen_seconds > 0:
                states = await h.collect_line_modes(args.listen_seconds)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if sys.platform == "darwin" and "was not found" in str(exc):
            print(
                "HINT: Use scan mode to discover the current CoreBluetooth UUID and pass it in --mac.",
                file=sys.stderr,
            )
        return 1

    sorted_states = [states[k] for k in sorted(states)]
    if args.line is not None:
        sorted_states = [s for s in sorted_states if s.line_id == args.line]

    if not sorted_states:
        if not args.allow_empty:
            print(
                "ERROR: no line-state updates received. "
                "Try a longer --listen-seconds, --trigger-get-info, or run a line command first.",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps({"line_states": []}, indent=2, sort_keys=True))
        else:
            print("no line-state updates received")
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "line_states": [
                        {
                            "line": s.line_id,
                            "on": s.on,
                            "output_mode": s.output_mode,
                            "output_state": s.output_state,
                            "output_rtc_timer": s.output_rtc_timer,
                        }
                        for s in sorted_states
                    ]
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("line states:")
        for state in sorted_states:
            print(f"  {format_line_mode_state(state)}")

    return 0


def main() -> int:
    args = parse_args()

    if args.command == "selftest":
        return run_selftest()

    if args.command == "scan":
        return asyncio.run(run_scan(args))

    if args.command == "line":
        return asyncio.run(run_line(args))

    if args.command == "query":
        return asyncio.run(run_query(args))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
