#!/usr/bin/env python3
"""Interactive setup wizard for in-lite Smart Hub-150 ESPHome bridge config.

Flow:
1) Ask for email and request login code from in-lite API.
2) Ask for code and fetch gardens.
3) Let user select a garden when multiple are returned.
4) Print selected hub id + passphrase hex.
5) Generate a full ESPHome YAML file with the selected values.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUTHORIZE_URL = "https://api.inlite.coffeeit.nl/v2/users/authorize"
LOGIN_URL = "https://api.inlite.coffeeit.nl/user/login"


def fail(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def slugify_name(name: str, fallback: str = "inlite_smart_hub_150_bridge") -> str:
    raw = name.strip().lower()
    if not raw:
        return fallback
    raw = raw.replace(" ", "_").replace("-", "_")
    raw = re.sub(r"[^a-z0-9_]", "", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        return fallback
    if not raw[0].isalpha():
        raw = f"inlite_{raw}"
    return raw


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    return default or ""


@dataclass
class CurlJsonResult:
    obj: dict[str, Any]
    request_path: Path | None
    raw_response_path: Path | None
    parsed_response_path: Path | None


def write_diag_file(diag_dir: Path | None, filename: str, content: str) -> Path | None:
    if diag_dir is None:
        return None
    diag_dir.mkdir(parents=True, exist_ok=True)
    out = diag_dir / filename
    out.write_text(content)
    return out


def run_curl_json(
    url: str,
    payload: dict[str, Any],
    *,
    allow_empty: bool = False,
    diag_dir: Path | None = None,
    diag_prefix: str = "response",
) -> CurlJsonResult:
    request_text = json.dumps(payload, separators=(",", ":"))
    request_path = write_diag_file(diag_dir, f"{diag_prefix}_request.json", request_text)

    cmd = [
        "curl",
        "-sS",
        url,
        "-H",
        "Content-Type: application/json",
        "--data",
        request_text,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    raw_path = write_diag_file(
        diag_dir,
        f"{diag_prefix}_response.raw.txt",
        proc.stdout,
    )
    if proc.returncode != 0:
        diag_hint = f" (raw response: {raw_path})" if raw_path is not None else ""
        fail(
            f"curl failed for {url}: {proc.stderr.strip() or f'exit {proc.returncode}'}{diag_hint}"
        )
    text = proc.stdout.strip()
    if not text:
        if allow_empty:
            parsed_path = write_diag_file(diag_dir, f"{diag_prefix}_response.parsed.json", "{}")
            return CurlJsonResult(
                obj={},
                request_path=request_path,
                raw_response_path=raw_path,
                parsed_response_path=parsed_path,
            )
        fail(f"empty response from {url}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        diag_hint = f" (raw response: {raw_path})" if raw_path is not None else ""
        fail(f"non-JSON response from {url}: {text[:200]}{diag_hint}")
    if not isinstance(obj, dict):
        fail(f"unexpected JSON shape from {url}")
    parsed_path = write_diag_file(
        diag_dir,
        f"{diag_prefix}_response.parsed.json",
        json.dumps(obj, indent=2),
    )
    return CurlJsonResult(
        obj=obj,
        request_path=request_path,
        raw_response_path=raw_path,
        parsed_response_path=parsed_path,
    )


def parse_mesh_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value if 0 <= value <= 0xFFFF else None

    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    if raw.lower().startswith("0x"):
        try:
            parsed = int(raw, 16)
        except ValueError:
            return None
        return parsed if 0 <= parsed <= 0xFFFF else None

    if raw.isdigit():
        parsed = int(raw, 10)
        return parsed if 0 <= parsed <= 0xFFFF else None

    return None


def parse_lines(value: str) -> list[int]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        fail("line list cannot be empty")
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            fail(f"invalid line value '{p}', expected integers")
        v = int(p, 10)
        if v < 1 or v > 16:
            fail(f"line {v} out of range (expected 1..16)")
        if v not in out:
            out.append(v)
    return out


def b64_api_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def yaml_q(value: str) -> str:
    # JSON string quoting is valid YAML flow scalar quoting.
    return json.dumps(value)


def generate_yaml(
    *,
    device_name: str,
    friendly_name: str,
    wifi_ssid: str,
    wifi_password: str,
    api_key: str,
    ota_password: str,
    hub_id_hex: str,
    passphrase_hex: str,
    lines: list[int],
) -> str:
    light_blocks = []
    for line in lines:
        light_blocks.append(
            "\n".join(
                [
                    "  - platform: inlite_hub",
                    "    inlite_hub_id: inlite_hub_main",
                    f"    line: {line}",
                    f"    name: In-lite Line {line}",
                ]
            )
        )

    return "\n".join(
        [
            "substitutions:",
            f"  device_name: {device_name}",
            f"  friendly_name: {yaml_q(friendly_name)}",
            "",
            "esphome:",
            "  name: ${device_name}",
            "  friendly_name: ${friendly_name}",
            "  comment: Local BLE bridge for in-lite Smart Hub-150 (FW 52) using inlite_hub external component.",
            "  project:",
            "    name: local.inlite_smart_hub_150_bridge",
            "    version: \"0.1.0\"",
            "",
            "esp32:",
            "  board: esp32dev",
            "  framework:",
            "    type: esp-idf",
            "",
            "logger:",
            "  level: INFO",
            "  logs:",
            "    inlite_hub: INFO",
            "    esp32_ble_client: INFO",
            "",
            "api:",
            "  encryption:",
            f"    key: {yaml_q(api_key)}",
            "",
            "ota:",
            "  - platform: esphome",
            f"    password: {yaml_q(ota_password)}",
            "",
            "wifi:",
            f"  ssid: {yaml_q(wifi_ssid)}",
            f"  password: {yaml_q(wifi_password)}",
            "  ap:",
            "    ssid: ${device_name} Fallback",
            "    password: CHANGE_ME_12345",
            "",
            "captive_portal:",
            "",
            "external_components:",
            "  - source:",
            "      type: git",
            "      url: https://github.com/derwasp/esphome-in-lite",
            "      ref: main",
            "    components: [inlite_hub]",
            "",
            "esp32_ble:",
            "  max_connections: 1",
            "",
            "esp32_ble_tracker:",
            "  id: inlite_ble_tracker_id",
            "  scan_parameters:",
            "    interval: 1100ms",
            "    window: 1100ms",
            "    active: false",
            "",
            "ble_client:",
            "  # Placeholder address; actual target is selected by inlite_hub autodiscovery.",
            "  - mac_address: \"00:00:00:00:00:00\"",
            "    id: inlite_ble_id",
            "    auto_connect: false",
            "",
            "inlite_hub:",
            "  - id: inlite_hub_main",
            "    ble_client_id: inlite_ble_id",
            "    esp32_ble_id: inlite_ble_tracker_id",
            f"    hub_id: {hub_id_hex}",
            f"    network_passphrase_hex: {yaml_q(passphrase_hex)}",
            "    auto_discover: true",
            "    discover_name_filter: inlite",
            "    command_timeout: 600ms",
            "    retries: 2",
            "    poll_interval: 15s",
            "",
            "light:",
            *light_blocks,
            "",
            "sensor:",
            "  - platform: inlite_hub",
            "    inlite_hub_id: inlite_hub_main",
            "    rssi:",
            "      name: In-lite BLE RSSI",
            "      entity_category: diagnostic",
            "    last_command_status:",
            "      name: In-lite Last Command Status",
            "      entity_category: diagnostic",
            "",
            "binary_sensor:",
            "  - platform: inlite_hub",
            "    inlite_hub_id: inlite_hub_main",
            "    connected:",
            "      name: In-lite BLE Connected",
            "      entity_category: diagnostic",
            "",
            "button:",
            "  - platform: restart",
            "    name: In-lite Bridge Restart",
            "    entity_category: diagnostic",
            "",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive in-lite API login + ESPHome YAML generator"
    )
    parser.add_argument("--email", default=None, help="in-lite account email")
    parser.add_argument(
        "--code", default=None, help="one-time login code (if omitted, prompt interactively)"
    )
    parser.add_argument(
        "--garden-index",
        type=int,
        default=None,
        help="1-based garden index to select (if omitted and multiple, prompt interactively)",
    )
    parser.add_argument(
        "--lines",
        default="1,2,3",
        help="comma-separated line numbers to generate (default: 1,2,3)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="output YAML file path (default: generated/<device_name>.yaml)",
    )
    parser.add_argument("--device-name", default=None, help="ESPHome node name")
    parser.add_argument("--friendly-name", default=None, help="ESPHome friendly name")
    parser.add_argument("--wifi-ssid", default=None, help="WiFi SSID for generated YAML")
    parser.add_argument("--wifi-password", default=None, help="WiFi password for generated YAML")
    parser.add_argument(
        "--api-key",
        default=None,
        help="Home Assistant API encryption key (base64). Default: generated.",
    )
    parser.add_argument(
        "--ota-password",
        default=None,
        help="OTA password. Default: generated.",
    )
    parser.add_argument(
        "--save-login-json",
        default=None,
        help="optional path to save full /user/login response JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    diag_dir = Path(".inlite_wizard") / f"run_{run_id}_{os.getpid()}"
    diag_dir.mkdir(parents=True, exist_ok=True)
    print(f"Diagnostics directory: {diag_dir}")

    email = (args.email or prompt("in-lite email")).strip()
    if not email:
        fail("email is required")

    print(f"Requesting login code for {email} ...")
    auth_result = run_curl_json(
        AUTHORIZE_URL,
        {"email": email, "language": "en"},
        allow_empty=True,
        diag_dir=diag_dir,
        diag_prefix="authorize",
    )
    auth_resp = auth_result.obj
    if "error" in auth_resp:
        fail(f"authorize failed: {auth_resp.get('error')}")
    print("Login code requested. Check your email.")
    if auth_result.raw_response_path is not None:
        print(f"Raw authorize response: {auth_result.raw_response_path}")

    code = (args.code or prompt("Enter login code from email")).strip()
    if not code:
        fail("login code is required")

    print("Logging in and fetching gardens ...")
    login_result = run_curl_json(
        LOGIN_URL,
        {"email": email, "code": code},
        diag_dir=diag_dir,
        diag_prefix="login",
    )
    login_resp = login_result.obj
    if "error" in login_resp:
        fail(f"login failed: {login_resp.get('error')}")
    if login_result.raw_response_path is not None:
        print(f"Raw login response: {login_result.raw_response_path}")

    if args.save_login_json:
        save_path = Path(args.save_login_json)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(login_resp, indent=2))
        print(f"Saved login JSON to {save_path}")

    gardens = login_resp.get("gardens")
    if not isinstance(gardens, list) or not gardens:
        fail("no gardens found in login response")

    parsed_gardens: list[dict[str, Any]] = []
    for g in gardens:
        if not isinstance(g, dict):
            continue
        pwd = g.get("password")
        if not isinstance(pwd, str):
            continue
        garden_name = str(g.get("name") or "garden")
        passphrase_hex = pwd.encode("utf-8").hex()

        transformers = g.get("transformers")
        candidate_hubs: list[dict[str, Any]] = []
        if isinstance(transformers, list):
            for t in transformers:
                if not isinstance(t, dict):
                    continue
                hub_id = None
                for key in ("deviceId", "hubId", "meshId"):
                    hub_id = parse_mesh_id(t.get(key))
                    if hub_id is not None:
                        break
                if hub_id is None:
                    continue
                candidate_hubs.append(
                    {
                        "hub_id": hub_id,
                        "hub_name": str(t.get("name") or ""),
                    }
                )

        if not candidate_hubs:
            # Backward compatibility if API shape differs.
            fallback_id = parse_mesh_id(g.get("_id"))
            if fallback_id is not None:
                candidate_hubs.append({"hub_id": fallback_id, "hub_name": ""})

        for hub in candidate_hubs:
            parsed_gardens.append(
                {
                    "name": garden_name,
                    "hub_name": hub["hub_name"],
                    "hub_id": hub["hub_id"],
                    "hub_id_hex": f"0x{hub['hub_id']:04X}",
                    "passphrase_hex": passphrase_hex,
                }
            )

    if not parsed_gardens:
        fail("no valid gardens with id/password in login response")

    print("\nAvailable gardens:")
    for idx, g in enumerate(parsed_gardens, start=1):
        hub_label = f" ({g['hub_name']})" if g.get("hub_name") else ""
        print(
            f"  {idx}. {g['name']}{hub_label}  hub_id={g['hub_id_hex']}  passphrase_hex={g['passphrase_hex']}"
        )

    if args.garden_index is not None:
        selected_index = args.garden_index
    elif len(parsed_gardens) == 1:
        selected_index = 1
    else:
        selected_raw = prompt("Select garden number", "1")
        try:
            selected_index = int(selected_raw)
        except ValueError:
            fail(f"invalid garden selection '{selected_raw}', expected a number")

    if selected_index < 1 or selected_index > len(parsed_gardens):
        fail(f"garden index out of range: {selected_index}")

    selected = parsed_gardens[selected_index - 1]
    print("\nSelected garden:")
    print(f"  name={selected['name']}")
    print(f"  hub_id={selected['hub_id_hex']}")
    print(f"  passphrase_hex={selected['passphrase_hex']}")

    lines = parse_lines(args.lines)

    default_device_name = slugify_name(f"inlite_{selected['name']}_bridge")
    if args.device_name is None:
        device_name_input = prompt("Device name", default_device_name).strip()
    else:
        device_name_input = args.device_name.strip()
    if not device_name_input:
        fail("device name cannot be empty")
    device_name = slugify_name(device_name_input, fallback=default_device_name)

    default_friendly = f"in-lite {selected['name']} Bridge"
    if args.friendly_name is None:
        friendly_name = prompt("Friendly name", default_friendly).strip() or default_friendly
    else:
        friendly_name = args.friendly_name.strip() or default_friendly

    if args.wifi_ssid is None:
        wifi_ssid = prompt("WiFi SSID", "CHANGE_ME_WIFI_SSID").strip()
    else:
        wifi_ssid = args.wifi_ssid.strip()
    if args.wifi_password is None:
        wifi_password = prompt("WiFi password", "CHANGE_ME_WIFI_PASSWORD").strip()
    else:
        wifi_password = args.wifi_password.strip()

    if args.api_key is None:
        api_key = prompt("API encryption key (base64)", b64_api_key()).strip()
    else:
        api_key = args.api_key.strip()
    if args.ota_password is None:
        ota_password = prompt("OTA password", secrets.token_urlsafe(18)).strip()
    else:
        ota_password = args.ota_password.strip()

    default_output = f"generated/{device_name}.yaml"
    if args.output is None:
        output_value = prompt("Output YAML path", default_output).strip() or default_output
    else:
        output_value = args.output.strip() or default_output
    output_path = Path(output_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    yaml_text = generate_yaml(
        device_name=device_name,
        friendly_name=friendly_name,
        wifi_ssid=wifi_ssid,
        wifi_password=wifi_password,
        api_key=api_key,
        ota_password=ota_password,
        hub_id_hex=selected["hub_id_hex"],
        passphrase_hex=selected["passphrase_hex"],
        lines=lines,
    )
    output_path.write_text(yaml_text)

    print("\nGenerated files/values:")
    print(f"  yaml={output_path}")
    print(f"  diagnostics_dir={diag_dir}")
    if auth_result.raw_response_path is not None:
        print(f"  authorize_raw={auth_result.raw_response_path}")
    if login_result.raw_response_path is not None:
        print(f"  login_raw={login_result.raw_response_path}")
    if login_result.parsed_response_path is not None:
        print(f"  login_parsed={login_result.parsed_response_path}")
    print(f"  hub_id={selected['hub_id_hex']}")
    print(f"  passphrase_hex={selected['passphrase_hex']}")
    print(f"  lines={','.join(str(x) for x in lines)}")

    print("\nNext:")
    print(f"  esphome config {output_path}")
    print(f"  esphome compile {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
