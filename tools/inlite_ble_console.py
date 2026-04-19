#!/usr/bin/env python3
"""Interactive curses console for the in-lite Smart Hub-150 BLE harness."""

from __future__ import annotations

import argparse
import asyncio
import curses
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from inlite_ble_harness import (
    END_ACK_MAGIC,
    InliteBleHarness,
    InliteCrypto,
    LineModeState,
    PKT_TYPE_ACK,
    PKT_TYPE_BLOCK_DATA,
    PKT_TYPE_DATA,
    PKT_TYPE_START_FLUSH,
    PKT_TYPE_STREAM_DATA_ALT,
    UUID_MESH_SERVICE,
    cmd_get_info_devices,
    cmd_set_outlet_mode,
    discover_candidates,
    parse_passphrase_hex,
    parse_block_line_mode_updates,
)


ENV_PASSPHRASE_HEX = "INLITE_PASSPHRASE_HEX"
ENV_HUB_ID = "INLITE_HUB_ID"
CONTROLLED_LINES = (0, 1, 2)
UI_TICK_S = 0.02
LOG_BUFFER_SIZE = 500
STATE_LINE_RE = re.compile(
    r"line=(\d+) on=(true|false) mode=0x([0-9a-fA-F]{2}) state=0x([0-9a-fA-F]{2}) rtc=(\d+)"
)
RX_PACKET_RE = re.compile(
    r"src=0x([0-9a-fA-F]+) dst=0x([0-9a-fA-F]+) type=(\d+) payload=([0-9a-fA-F]*)"
)

STATE_DISCONNECTED = "Disconnected"
STATE_SCANNING = "Scanning"
STATE_CONNECTING = "Connecting"
STATE_CONNECTED = "Connected"
STATE_DISCONNECTING = "Disconnecting"
STATE_ERROR = "Error"


@dataclass(frozen=True)
class QueuedCommand:
    id: int
    kind: str
    created_at: float
    line_id: Optional[int] = None
    desired_on: Optional[bool] = None


class UiInliteBleHarness(InliteBleHarness):
    def __init__(self, *args, log_sink, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._log_sink = log_sink
        self.current_command_id: Optional[int] = None

    def _log(self, msg: str) -> None:
        self._log_sink(msg, self.current_command_id)


class HarnessAdapter:
    def __init__(
        self,
        *,
        hub_id: int,
        controller_id: int,
        passphrase: bytes,
        timeout_ms: int,
        retries: int,
        write_with_response: bool,
        log_sink,
    ) -> None:
        self.hub_id = hub_id
        self.controller_id = controller_id
        self.passphrase = passphrase
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.write_with_response = write_with_response
        self._log_sink = log_sink
        self._harness: Optional[UiInliteBleHarness] = None
        self.connected = False
        self.address: Optional[str] = None

    async def connect(self, address: str) -> None:
        if self._harness is not None:
            await self.disconnect()

        harness = UiInliteBleHarness(
            mac=address,
            hub_id=self.hub_id,
            crypto=InliteCrypto(self.passphrase, controller_id=self.controller_id),
            timeout_ms=self.timeout_ms,
            retries=self.retries,
            write_with_response=self.write_with_response,
            verbose=False,
            log_sink=self._log_sink,
        )
        try:
            await harness.__aenter__()
        except Exception:
            self._harness = None
            self.connected = False
            self.address = None
            raise

        self._harness = harness
        self.connected = True
        self.address = address

    async def disconnect(self) -> None:
        harness = self._harness
        self._harness = None
        self.connected = False
        self.address = None
        if harness is None:
            return
        await harness.__aexit__(None, None, None)

    def is_connected(self) -> bool:
        harness = self._harness
        if harness is None:
            return False
        return bool(getattr(harness.client, "is_connected", False))

    def get_line_modes_snapshot(self) -> dict[int, LineModeState]:
        harness = self._harness
        if harness is None:
            return {}
        return harness.get_line_modes_snapshot()

    async def send_stream(self, payload: bytes, command_id: Optional[int] = None) -> None:
        harness = self._harness
        if harness is None:
            raise RuntimeError("not connected")
        harness.current_command_id = command_id
        try:
            await harness.send_stream(payload)
        finally:
            harness.current_command_id = None


class InliteBleConsole:
    def __init__(self, stdscr, args: argparse.Namespace, passphrase: bytes) -> None:
        self.stdscr = stdscr
        self.args = args
        self.passphrase = passphrase
        self.controller_id = (
            args.controller_id
            if args.controller_id is not None
            else random.randint(32768, 65533)
        )
        self.adapter = HarnessAdapter(
            hub_id=args.hub_id,
            controller_id=self.controller_id,
            passphrase=passphrase,
            timeout_ms=args.timeout_ms,
            retries=args.retries,
            write_with_response=args.write_with_response,
            log_sink=self._handle_harness_log,
        )
        self.session_lock = asyncio.Lock()
        self.queue_condition = asyncio.Condition()
        self.command_queue: deque[QueuedCommand] = deque()
        self.pending_commands: deque[QueuedCommand] = deque()
        self.inflight_command: Optional[QueuedCommand] = None
        self.command_counter = 0
        self.pending_counts = {line: 0 for line in CONTROLLED_LINES}
        self.projected_states = {line: None for line in CONTROLLED_LINES}
        self.known_states: dict[int, LineModeState] = {}
        self.log_lines: deque[str] = deque(maxlen=LOG_BUFFER_SIZE)
        self.connection_state = STATE_DISCONNECTED
        self.last_result = "Idle"
        self.explicit_address = args.mac
        self.selected_address = args.mac
        self.stop_requested = False
        self.worker_task: Optional[asyncio.Task[None]] = None
        self.operation_task: Optional[asyncio.Task[None]] = None
        self._last_draw_error: Optional[str] = None

    async def run(self) -> int:
        self._setup_curses()
        self._log("INFO", f"Hub ID source: {self._hub_id_source()}")
        self._log(
            "INFO",
            f"Hub ID 0x{self.args.hub_id:04x} is the mesh destination ID used by the Smart Hub; "
            "the wizard prints this value as hub_id for the selected garden.",
        )
        self._log("INFO", f"Passphrase source: {self._passphrase_source()}")
        if self.explicit_address:
            self._log("INFO", f"Configured target address: {self.explicit_address}")
        else:
            self._log(
                "INFO",
                "No explicit target address configured. Press S to scan or C to autodiscover.",
            )
        self.worker_task = asyncio.create_task(self._transport_worker())

        try:
            while not self.stop_requested:
                await self._drain_input()
                await self._check_operation_task()
                await self._poll_connection_state()
                self._refresh_known_states()
                self._draw()
                await asyncio.sleep(UI_TICK_S)
        finally:
            await self._shutdown()

        return 0

    def _setup_curses(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)

    def _passphrase_source(self) -> str:
        if self.args.passphrase_hex:
            return f"--passphrase-hex (overrides {ENV_PASSPHRASE_HEX})"
        return ENV_PASSPHRASE_HEX

    def _hub_id_source(self) -> str:
        if self.args.hub_id_from_flag:
            return f"--hub-id (overrides {ENV_HUB_ID})"
        return ENV_HUB_ID

    async def _shutdown(self) -> None:
        self.stop_requested = True
        if self.operation_task is not None and not self.operation_task.done():
            self.operation_task.cancel()
            try:
                await self.operation_task
            except asyncio.CancelledError:
                pass
        await self._clear_queued_commands("shutdown", log_prefix="INFO")
        async with self.session_lock:
            await self.adapter.disconnect()
        if self.worker_task is not None:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

    async def _drain_input(self) -> None:
        while True:
            key = self.stdscr.getch()
            if key == -1:
                return
            await self._handle_key(key)

    async def _handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q")):
            self.stop_requested = True
            self.last_result = "Quit requested"
            return

        if key in (ord("l"), ord("L")):
            self.log_lines.clear()
            self._log("INFO", "Log cleared")
            self.last_result = "Log cleared"
            return

        if key == curses.KEY_RESIZE:
            return

        if key in (ord("c"), ord("C")):
            self._start_operation(self._connect_flow(), "connect")
            return

        if key in (ord("d"), ord("D")):
            self._start_operation(self._disconnect_flow("user request"), "disconnect")
            return

        if key in (ord("s"), ord("S")):
            self._start_operation(self._scan_flow(), "scan")
            return

        if key in (ord("r"), ord("R")):
            await self._enqueue_refresh(reason="manual refresh")
            return

        if key in (ord("1"), ord("2"), ord("3")):
            line_id = CONTROLLED_LINES[key - ord("1")]
            await self._enqueue_toggle(line_id)

    def _start_operation(self, coro, label: str) -> None:
        if self.operation_task is not None and not self.operation_task.done():
            self._log("INFO", f"Ignoring {label}; another operation is still running")
            self.last_result = f"Busy with {self.connection_state.lower()}"
            return
        self.operation_task = asyncio.create_task(coro)

    async def _check_operation_task(self) -> None:
        if self.operation_task is None or not self.operation_task.done():
            return
        task = self.operation_task
        self.operation_task = None
        try:
            await task
        except asyncio.CancelledError:
            self._log("INFO", "Background operation cancelled")
        except Exception as exc:
            self.connection_state = STATE_ERROR
            self.last_result = f"Operation failed: {exc}"
            self._log("ERROR", f"Background operation failed: {exc}")

    async def _connect_flow(self) -> None:
        if self.adapter.is_connected():
            self._log("INFO", "Already connected")
            self.connection_state = STATE_CONNECTED
            self.last_result = "Already connected"
            return

        target = self.explicit_address or self.selected_address
        if not target:
            target = await self._scan_for_target(log_selection=True)
            if not target:
                return

        self.connection_state = STATE_CONNECTING
        self.last_result = f"Connecting to {target}"
        self._log(
            "INFO",
            f"Connecting address={target} hub_id=0x{self.args.hub_id:04x} "
            f"controller_id=0x{self.controller_id:04x}",
        )

        try:
            async with self.session_lock:
                await self.adapter.connect(target)
        except Exception as exc:
            self.connection_state = STATE_ERROR
            self.last_result = f"Connect failed: {exc}"
            self._log("ERROR", f"Connect failed: {exc}")
            return

        self.selected_address = target
        self.connection_state = STATE_CONNECTED
        self.last_result = f"Connected to {target}"
        self._log("INFO", f"Connected to {target}")
        await self._enqueue_refresh(reason="initial refresh")

    async def _disconnect_flow(self, reason: str) -> None:
        if not self.adapter.connected and not self.adapter.is_connected():
            if self.connection_state not in {STATE_DISCONNECTED, STATE_ERROR}:
                self.connection_state = STATE_DISCONNECTED
            self.last_result = "Already disconnected"
            self._log("INFO", "Already disconnected")
            return

        self.connection_state = STATE_DISCONNECTING
        self.last_result = "Disconnecting"
        cleared = await self._clear_queued_commands("disconnect requested", log_prefix="INFO")
        if cleared:
            self._log("INFO", f"Discarded {cleared} queued command(s) before disconnect")

        try:
            async with self.session_lock:
                await self.adapter.disconnect()
        except Exception as exc:
            self.connection_state = STATE_ERROR
            self.last_result = f"Disconnect failed: {exc}"
            self._log("ERROR", f"Disconnect failed: {exc}")
            return

        self.connection_state = STATE_DISCONNECTED
        self.last_result = "Disconnected"
        self._log("INFO", f"Disconnected ({reason})")
        self._refresh_known_states()
        self._recompute_projected_states()

    async def _handle_transport_disconnect(self, reason: str) -> None:
        if not self.adapter.connected and not self.adapter.is_connected():
            return

        cleared = await self._clear_queued_commands(reason, log_prefix="ERROR")
        if cleared:
            self._log("ERROR", f"Cleared {cleared} queued command(s) after disconnect")

        try:
            async with self.session_lock:
                await self.adapter.disconnect()
        except Exception:
            pass

        self.connection_state = STATE_DISCONNECTED
        self.last_result = reason
        self._log("ERROR", reason)
        self._recompute_projected_states()

    async def _scan_flow(self) -> None:
        if self.adapter.is_connected():
            self._log("INFO", "Disconnect before scanning for a new target")
            self.last_result = "Scan skipped while connected"
            return

        await self._scan_for_target(log_selection=True)

    async def _scan_for_target(self, *, log_selection: bool) -> Optional[str]:
        self.connection_state = STATE_SCANNING
        self.last_result = "Scanning"
        self._log(
            "INFO",
            f"Scanning {self.args.discover_seconds:.1f}s (name~{self.args.discover_name_filter!r}, service={UUID_MESH_SERVICE})",
        )

        try:
            rows = await discover_candidates(
                seconds=self.args.discover_seconds,
                name_filter=self.args.discover_name_filter,
                match_address=self.args.discover_match_address or self.selected_address,
            )
        except Exception as exc:
            self.connection_state = STATE_ERROR
            self.last_result = f"Scan failed: {exc}"
            self._log("ERROR", f"Scan failed: {exc}")
            return None

        if not rows:
            self.connection_state = STATE_DISCONNECTED
            self.last_result = "No target found"
            self._log("ERROR", "No matching in-lite hubs found")
            return None

        best = rows[0]
        discovered_address = best["address"]
        rssi = "?" if best["rssi"] is None else str(best["rssi"])
        if self.explicit_address:
            self._log(
                "INFO",
                f"Best discovered address={discovered_address} name={best['name']} rssi={rssi}; explicit --mac remains active",
            )
            selected = self.explicit_address
        else:
            self.selected_address = discovered_address
            selected = discovered_address
            if log_selection:
                self._log(
                    "INFO",
                    f"Selected address={discovered_address} name={best['name']} rssi={rssi}",
                )

        self.connection_state = STATE_DISCONNECTED
        self.last_result = f"Scan selected {selected}"
        return selected

    async def _enqueue_toggle(self, line_id: int) -> None:
        if not self.adapter.is_connected():
            self._log("ERROR", f"Rejected toggle for line {line_id}: not connected")
            self.last_result = "Toggle rejected while disconnected"
            return

        current = self.projected_states.get(line_id)
        desired_on = True if current is None else not current
        command = self._next_command(
            kind="line",
            line_id=line_id,
            desired_on=desired_on,
        )
        await self._enqueue_command(command)
        self.last_result = f"Queued #{command.id} line {line_id} -> {self._state_word(desired_on)}"
        self._log(
            "CMD",
            f"#{command.id} queued line={line_id} -> {self._state_word(desired_on)}",
        )

    async def _enqueue_refresh(self, reason: str) -> None:
        if not self.adapter.is_connected():
            self._log("ERROR", f"Rejected refresh ({reason}): not connected")
            self.last_result = "Refresh rejected while disconnected"
            return

        command = self._next_command(kind="refresh")
        await self._enqueue_command(command)
        self.last_result = f"Queued #{command.id} refresh"
        self._log("CMD", f"#{command.id} queued refresh ({reason})")

    def _next_command(
        self,
        *,
        kind: str,
        line_id: Optional[int] = None,
        desired_on: Optional[bool] = None,
    ) -> QueuedCommand:
        self.command_counter += 1
        return QueuedCommand(
            id=self.command_counter,
            kind=kind,
            created_at=time.monotonic(),
            line_id=line_id,
            desired_on=desired_on,
        )

    async def _enqueue_command(self, command: QueuedCommand) -> None:
        async with self.queue_condition:
            self.command_queue.append(command)
            self.pending_commands.append(command)
            self._recompute_projected_states()
            self.queue_condition.notify()

    async def _clear_queued_commands(self, reason: str, log_prefix: str) -> int:
        async with self.queue_condition:
            if not self.command_queue:
                return 0
            cleared = list(self.command_queue)
            self.command_queue.clear()
            self.queue_condition.notify_all()

        cleared_ids = {command.id for command in cleared}
        self.pending_commands = deque(
            command for command in self.pending_commands if command.id not in cleared_ids
        )
        self._recompute_projected_states()
        self._log(log_prefix, f"Discarded queued commands: {reason}")
        return len(cleared)

    async def _transport_worker(self) -> None:
        while True:
            async with self.queue_condition:
                await self.queue_condition.wait_for(
                    lambda: self.stop_requested or bool(self.command_queue)
                )
                if self.stop_requested:
                    return
                command = self.command_queue.popleft()

            self.inflight_command = command
            self._recompute_projected_states()

            try:
                payload = self._payload_for_command(command)
                self.last_result = f"Sending #{command.id}"
                self._log("CMD", self._describe_send(command))
                async with self.session_lock:
                    if not self.adapter.is_connected():
                        raise RuntimeError("not connected")
                    await self.adapter.send_stream(payload, command_id=command.id)
                self.last_result = f"Sent #{command.id}"
                self._log("CMD", self._describe_success(command))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_result = f"Failed #{command.id}: {exc}"
                self._log("ERROR", f"#{command.id} failed: {exc}")
                if not self.adapter.is_connected():
                    await self._handle_transport_disconnect("BLE connection lost")
            finally:
                self._retire_command(command)

    def _payload_for_command(self, command: QueuedCommand) -> bytes:
        if command.kind == "line":
            assert command.line_id is not None
            assert command.desired_on is not None
            return cmd_set_outlet_mode(command.line_id, command.desired_on)
        if command.kind == "refresh":
            return cmd_get_info_devices()
        raise ValueError(f"unknown command kind: {command.kind}")

    def _retire_command(self, command: QueuedCommand) -> None:
        self.inflight_command = None
        if self.pending_commands and self.pending_commands[0].id == command.id:
            self.pending_commands.popleft()
        else:
            self.pending_commands = deque(
                pending for pending in self.pending_commands if pending.id != command.id
            )
        self._recompute_projected_states()

    def _refresh_known_states(self) -> None:
        if not self.adapter.connected and not self.adapter.is_connected():
            return
        snapshot = self.adapter.get_line_modes_snapshot()
        filtered = {line: snapshot[line] for line in CONTROLLED_LINES if line in snapshot}
        if filtered == self.known_states:
            return
        self.known_states = filtered
        self._recompute_projected_states()

    def _recompute_projected_states(self) -> None:
        projected = {
            line: (self.known_states[line].on if line in self.known_states else None)
            for line in CONTROLLED_LINES
        }
        pending_counts = {line: 0 for line in CONTROLLED_LINES}

        for command in self.pending_commands:
            if command.kind != "line" or command.line_id is None:
                continue
            pending_counts[command.line_id] += 1
            projected[command.line_id] = command.desired_on

        self.pending_counts = pending_counts
        self.projected_states = projected

    async def _poll_connection_state(self) -> None:
        if self.adapter.connected and not self.adapter.is_connected():
            await self._handle_transport_disconnect("BLE connection lost")

    def _describe_send(self, command: QueuedCommand) -> str:
        if command.kind == "line":
            return (
                f"#{command.id} sending line={command.line_id} -> "
                f"{self._state_word(command.desired_on)} (toggle request)"
            )
        return f"#{command.id} sending refresh (GET_INFO_DEVICES state sync request)"

    def _describe_success(self, command: QueuedCommand) -> str:
        if command.kind == "line":
            return (
                f"#{command.id} sent line={command.line_id} -> "
                f"{self._state_word(command.desired_on)} (accepted by BLE transport)"
            )
        return f"#{command.id} sent refresh (GET_INFO_DEVICES accepted by BLE transport)"

    def _packet_type_name(self, packet_type: int) -> str:
        if packet_type == PKT_TYPE_START_FLUSH:
            return "stream-control"
        if packet_type in {PKT_TYPE_DATA, PKT_TYPE_STREAM_DATA_ALT}:
            return "stream-data"
        if packet_type == PKT_TYPE_ACK:
            return "ack"
        if packet_type == PKT_TYPE_BLOCK_DATA:
            return "block-data"
        return f"type-{packet_type}"

    def _format_tx_log(self, body: str) -> tuple[str, str]:
        if match := re.fullmatch(r"START_FLUSH attempt=(\d+)", body):
            attempt = int(match.group(1))
            return (
                "TX",
                f"begin stream upload attempt={attempt} (tell the hub a new command payload is starting)",
            )

        if match := re.fullmatch(r"DATA offset=0x([0-9a-fA-F]+) len=(\d+) attempt=(\d+)", body):
            offset = int(match.group(1), 16)
            chunk_len = int(match.group(2))
            attempt = int(match.group(3))
            return (
                "TX",
                f"send stream chunk offset={offset} len={chunk_len} attempt={attempt} "
                f"(command bytes being uploaded to the hub)",
            )

        if match := re.fullmatch(r"END_FLUSH offset=0x([0-9a-fA-F]+) attempt=(\d+)", body):
            final_offset = int(match.group(1), 16)
            attempt = int(match.group(2))
            return (
                "TX",
                f"finish stream upload total_len={final_offset} attempt={attempt} "
                f"(hub should now have the full command payload)",
            )

        return "TX", body

    def _format_rx_log(self, body: str) -> tuple[str, str]:
        match = RX_PACKET_RE.fullmatch(body)
        if not match:
            return "RX", body

        src_id = int(match.group(1), 16)
        dst_id = int(match.group(2), 16)
        packet_type = int(match.group(3))
        payload_hex = match.group(4)

        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError:
            payload = b""

        if packet_type == PKT_TYPE_ACK and len(payload) >= 2:
            ack_offset = payload[0] | (payload[1] << 8)
            is_final = len(payload) >= 3 and payload[2] == END_ACK_MAGIC
            final_text = "yes" if is_final else "no"
            return (
                "ACK",
                f"hub 0x{src_id:04x} acknowledged stream bytes through offset={ack_offset} "
                f"(final_ack={final_text}, controller=0x{dst_id:04x})",
            )

        if packet_type == PKT_TYPE_BLOCK_DATA:
            updates = parse_block_line_mode_updates(payload)
            if updates:
                summary = ", ".join(
                    f"line {update.line_id}={self._state_word(update.on)}" for update in updates
                )
                return (
                    "RX",
                    f"hub 0x{src_id:04x} sent a state packet ({summary}) "
                    f"to controller 0x{dst_id:04x}",
                )
            return (
                "RX",
                f"hub 0x{src_id:04x} sent block-data to controller 0x{dst_id:04x} "
                f"(type=115, payload={payload_hex[:32]}{'...' if len(payload_hex) > 32 else ''}; "
                f"not a recognized line-state update)",
            )

        if packet_type == PKT_TYPE_START_FLUSH:
            offset_text = "?"
            if len(payload) >= 2:
                offset_text = str(payload[0] | (payload[1] << 8))
            return (
                "RX",
                f"hub 0x{src_id:04x} sent stream-control packet to controller 0x{dst_id:04x} "
                f"(type=112, offset={offset_text}; start/end of a reverse upload from the hub, "
                "so the controller must ACK it or the hub keeps retrying)",
            )

        if packet_type in {PKT_TYPE_DATA, PKT_TYPE_STREAM_DATA_ALT}:
            offset_text = "?"
            data_len = max(0, len(payload) - 2)
            offset = None
            if len(payload) >= 2:
                offset = payload[0] | (payload[1] << 8)
                offset_text = str(offset)
            payload_note = ""
            if offset == 0 and len(payload) >= 5:
                opcode = payload[3] | (payload[4] << 8)
                if opcode == 5:
                    payload_note = "; contains GET_INFO_DEVICES response data"
            return (
                "RX",
                f"hub 0x{src_id:04x} sent stream-data packet to controller 0x{dst_id:04x} "
                f"(offset={offset_text}, data_len={data_len}{payload_note})",
            )

        return (
            "RX",
            f"packet from hub 0x{src_id:04x} to controller 0x{dst_id:04x} "
            f"({self._packet_type_name(packet_type)}, payload={payload_hex})",
        )

    def _format_state_log(self, body: str) -> tuple[str, str]:
        updates = []
        for match in STATE_LINE_RE.finditer(body):
            line_id = int(match.group(1))
            on = match.group(2) == "true"
            mode = match.group(3).lower()
            state = match.group(4).lower()
            rtc = int(match.group(5))
            updates.append(
                f"line {line_id}={self._state_word(on)} (mode=0x{mode}, state=0x{state}, rtc={rtc})"
            )

        if updates:
            return "STATE", "hub line update: " + ", ".join(updates)
        return "STATE", body

    def _format_ack_log(self, body: str) -> tuple[str, str]:
        if match := re.fullmatch(
            r"sent reverse-stream (ACK|END_ACK) offset=0x([0-9a-fA-F]+) to 0x([0-9a-fA-F]+)",
            body,
        ):
            ack_kind = match.group(1)
            offset = int(match.group(2), 16)
            destination = int(match.group(3), 16)
            meaning = (
                "final reverse-stream acknowledgement"
                if ack_kind == "END_ACK"
                else "reverse-stream progress acknowledgement"
            )
            return (
                "ACK",
                f"controller sent {ack_kind} to hub 0x{destination:04x} for offset={offset} "
                f"({meaning})",
            )
        return "ACK", body

    def _handle_harness_log(self, msg: str, command_id: Optional[int]) -> None:
        if msg.startswith("[tx]"):
            prefix, body = self._format_tx_log(msg[4:].strip())
        elif msg.startswith("[rx]"):
            prefix, body = self._format_rx_log(msg[4:].strip())
        elif msg.startswith("[ack]"):
            prefix, body = self._format_ack_log(msg[5:].strip())
        elif msg.startswith("[state]"):
            prefix, body = self._format_state_log(msg[7:].strip())
        else:
            prefix = "INFO"
            body = msg

        if command_id is not None:
            body = f"#{command_id} {body}"
        self._log(prefix, body)

    def _log(self, prefix: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"{timestamp} {prefix:<5} {message}")

    def _state_word(self, value: Optional[bool]) -> str:
        if value is None:
            return "Unknown"
        return "On" if value else "Off"

    def _connection_color(self):
        if not curses.has_colors():
            return curses.A_BOLD
        if self.connection_state == STATE_CONNECTED:
            return curses.color_pair(1) | curses.A_BOLD
        if self.connection_state in {STATE_ERROR, STATE_DISCONNECTED}:
            return curses.color_pair(2) | curses.A_BOLD
        return curses.color_pair(3) | curses.A_BOLD

    def _state_color(self, value: Optional[bool]):
        if not curses.has_colors():
            return curses.A_NORMAL
        if value is None:
            return curses.color_pair(3)
        if value:
            return curses.color_pair(1)
        return curses.color_pair(2)

    def _draw(self) -> None:
        try:
            self._draw_impl()
            self._last_draw_error = None
        except curses.error as exc:
            text = str(exc) or "curses draw error"
            if text != self._last_draw_error:
                self._log("ERROR", text)
                self._last_draw_error = text

    def _draw_impl(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()

        status = (
            f"State: {self.connection_state}  "
            f"Target: {self.selected_address or '-'}  "
            f"Hub: 0x{self.args.hub_id:04x}  "
            f"Ctrl: 0x{self.controller_id:04x}  "
            f"Queue: {len(self.pending_commands)}"
        )
        self._addstr(0, 0, self._fit(status, width), self._connection_color())

        status2 = (
            f"Last: {self.last_result}  "
            f"Write mode: {'response' if self.args.write_with_response else 'no-response'}"
        )
        self._addstr(1, 0, self._fit(status2, width), curses.A_NORMAL)

        help_text = "Keys: C connect  D disconnect  1/2/3 toggle  R refresh  S scan  L clear log  Q quit"
        self._addstr(2, 0, self._fit(help_text, width), curses.A_DIM)

        header = "Key  Line  Known     Projected  Pending"
        self._addstr(4, 0, self._fit(header, width), curses.A_BOLD)

        for idx, line_id in enumerate(CONTROLLED_LINES):
            row = 5 + idx
            known = self.known_states.get(line_id)
            known_on = known.on if known is not None else None
            projected = self.projected_states.get(line_id)
            pending = self.pending_counts.get(line_id, 0)

            self._addstr(row, 0, f" {idx + 1}", curses.A_BOLD)
            self._addstr(row, 5, f"{line_id:>2}", curses.A_NORMAL)
            self._addstr(row, 11, f"{self._state_word(known_on):<8}", self._state_color(known_on))
            self._addstr(
                row,
                21,
                f"{self._state_word(projected):<9}",
                self._state_color(projected),
            )
            self._addstr(row, 33, f"{pending:<7}", curses.A_NORMAL)

        log_top = 9
        self._addstr(log_top, 0, self._fit("Bluetooth Log", width), curses.A_BOLD)
        separator = "-" * max(0, width - 1)
        self._addstr(log_top + 1, 0, separator, curses.A_DIM)

        available = max(0, height - (log_top + 2))
        if available:
            visible_lines = list(self.log_lines)[-available:]
            start_row = log_top + 2
            for offset, line in enumerate(visible_lines):
                self._addstr(start_row + offset, 0, self._fit(line, width), curses.A_NORMAL)

        self.stdscr.refresh()

    def _addstr(self, y: int, x: int, text: str, attr: int) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width or not text:
            return
        self.stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)

    def _fit(self, text: str, width: int) -> str:
        if width <= 1:
            return ""
        if len(text) < width:
            return text
        if width <= 4:
            return text[: max(0, width - 1)]
        return text[: width - 4] + "..."


def resolve_passphrase_hex(value: Optional[str]) -> bytes:
    if value:
        return parse_passphrase_hex(value)

    env_value = os.getenv(ENV_PASSPHRASE_HEX)
    if env_value:
        return parse_passphrase_hex(env_value)

    raise ValueError(
        f"provide --passphrase-hex or set {ENV_PASSPHRASE_HEX}"
    )


def resolve_hub_id(value: Optional[str]) -> tuple[int, bool]:
    if value:
        return int(value, 0), True

    env_value = os.getenv(ENV_HUB_ID)
    if env_value:
        return int(env_value, 0), False

    raise ValueError(
        f"provide --hub-id or set {ENV_HUB_ID}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive in-lite Smart Hub-150 BLE console"
    )
    parser.add_argument(
        "--mac",
        help="Smart Hub BLE address (MAC or CoreBluetooth UUID on macOS)",
    )
    parser.add_argument(
        "--hub-id",
        default=None,
        help=f"Smart Hub mesh ID (for example 0x1234, or set {ENV_HUB_ID})",
    )
    parser.add_argument(
        "--passphrase-hex",
        default=None,
        help=f"Network passphrase raw bytes as hex (or set {ENV_PASSPHRASE_HEX})",
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
    parser.add_argument(
        "--discover-seconds",
        type=float,
        default=12.0,
        help="Discovery scan duration in seconds",
    )
    parser.add_argument(
        "--discover-name-filter",
        default="inlite",
        help="Case-insensitive discovery name substring filter",
    )
    parser.add_argument(
        "--discover-match-address",
        default=None,
        help="Preferred address during autodiscovery (MAC or CoreBluetooth UUID)",
    )
    args = parser.parse_args()
    args.hub_id, args.hub_id_from_flag = resolve_hub_id(args.hub_id)
    return args


def run_curses_app(stdscr, args: argparse.Namespace, passphrase: bytes) -> int:
    return asyncio.run(InliteBleConsole(stdscr, args, passphrase).run())


def main() -> int:
    try:
        args = parse_args()
        passphrase = resolve_passphrase_hex(args.passphrase_hex)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        return curses.wrapper(run_curses_app, args, passphrase)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
