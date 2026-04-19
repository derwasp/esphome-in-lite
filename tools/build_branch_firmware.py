#!/usr/bin/env python3
"""Build ESPHome firmware with a branch-stamped project version."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_BASE_VERSION = "0.9.0"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    passthrough_args: list[str] = []
    if "--" in raw_args:
        separator_index = raw_args.index("--")
        passthrough_args = raw_args[separator_index + 1 :]
        raw_args = raw_args[:separator_index]

    parser = argparse.ArgumentParser(
        description=(
            "Create a temporary copy of an ESPHome YAML, stamp "
            "esphome.project.version as <base-version>-<branch>, then run ESPHome."
        )
    )
    parser.add_argument("config", help="Path to the ESPHome YAML configuration")
    parser.add_argument(
        "command",
        nargs="?",
        default="compile",
        help="ESPHome command to run, for example compile, upload, run, or config",
    )
    parser.add_argument(
        "--base-version",
        default=DEFAULT_BASE_VERSION,
        help=f"Base project version to prefix before the branch name (default: {DEFAULT_BASE_VERSION})",
    )
    parser.add_argument(
        "--branch",
        help="Override the git branch name used in the stamped project version",
    )
    parser.add_argument(
        "--esphome-bin",
        help="Override the ESPHome executable path (defaults to repo .venv/bin/esphome when present)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the generated temporary YAML after the ESPHome command exits",
    )
    args = parser.parse_args(raw_args)
    args.esphome_args = passthrough_args
    return args


def run_capture(cmd: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def repo_root(script_path: Path) -> Path:
    return Path(
        run_capture(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=script_path.parent,
        )
    )


def current_branch(root: Path) -> str:
    branch = run_capture(["git", "branch", "--show-current"], cwd=root)
    if not branch:
        raise SystemExit("Could not determine the current git branch")
    return branch


def sanitize_branch_name(branch: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-.")
    return sanitized or "detached"


def default_esphome_bin(root: Path) -> str:
    candidate = root / ".venv" / "bin" / "esphome"
    if candidate.exists():
        return str(candidate)
    return "esphome"


def rewrite_project_version(config_text: str, stamped_version: str) -> str:
    lines = config_text.splitlines(keepends=True)
    in_esphome = False
    esphome_indent = -1
    in_project = False
    project_indent = -1

    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)

        if not stripped or stripped.startswith("#"):
            continue

        if not in_esphome:
            if stripped.startswith("esphome:"):
                in_esphome = True
                esphome_indent = indent
            continue

        if indent <= esphome_indent:
            break

        if not in_project:
            if stripped.startswith("project:"):
                in_project = True
                project_indent = indent
            continue

        if indent <= project_indent:
            break

        if stripped.startswith("version:"):
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f'{" " * indent}version: "{stamped_version}"{newline}'
            return "".join(lines)

    raise SystemExit(
        "Could not find esphome.project.version in the YAML. "
        "Add an esphome -> project -> version field first."
    )

def main() -> int:
    args = parse_args()

    script_path = Path(__file__).resolve()
    root = repo_root(script_path)
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    branch_name = args.branch or current_branch(root)
    branch_tag = sanitize_branch_name(branch_name)
    stamped_version = f"{args.base_version}-{branch_tag}"
    esphome_bin = args.esphome_bin or default_esphome_bin(root)

    temp_path: Path | None = None
    try:
        rewritten = rewrite_project_version(
            config_path.read_text(encoding="utf-8"),
            stamped_version,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.stem}.branch-version.",
            suffix=config_path.suffix,
            delete=False,
        ) as handle:
            handle.write(rewritten)
            temp_path = Path(handle.name)

        print(f"Branch:          {branch_name}")
        print(f"Stamped version: {stamped_version}")
        print(f"ESPHome binary:  {esphome_bin}")
        print(f"Temp config:     {temp_path}")

        cmd = [esphome_bin, args.command, str(temp_path), *args.esphome_args]
        print("Running:", " ".join(cmd))
        completed = subprocess.run(cmd, cwd=root)
        return completed.returncode
    finally:
        if temp_path is not None and temp_path.exists() and not args.keep_temp:
            temp_path.unlink()


if __name__ == "__main__":
    sys.exit(main())
