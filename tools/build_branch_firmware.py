#!/usr/bin/env python3
"""Build ESPHome firmware with a branch-stamped project version."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_BASE_VERSION = "0.9.0"
SEMVER_TAG_RE = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


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
        help=(
            "Override the derived project version base. When omitted, exact tags "
            "use the tag version, otherwise the latest tag contributes the major "
            "and minor numbers while the patch number becomes the number of commits "
            f"since that tag. Falls back to {DEFAULT_BASE_VERSION} if the repo has no tags."
        ),
    )
    parser.add_argument(
        "--branch",
        help="Override the git branch name used in the stamped project version",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print the derived project version and exit without running ESPHome",
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
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(cmd)}: {message}")
    return result.stdout.strip()


def try_capture(cmd: list[str], *, cwd: Path) -> str | None:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def repo_root(script_path: Path) -> Path:
    try:
        return Path(
            run_capture(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=script_path.parent,
            )
        )
    except RuntimeError as err:
        raise SystemExit(str(err)) from err


def current_branch(root: Path) -> str:
    github_head_ref = os.environ.get("GITHUB_HEAD_REF")
    if github_head_ref:
        return github_head_ref

    github_ref_type = os.environ.get("GITHUB_REF_TYPE")
    github_ref_name = os.environ.get("GITHUB_REF_NAME")
    if github_ref_type == "branch" and github_ref_name:
        return github_ref_name

    try:
        branch = run_capture(["git", "branch", "--show-current"], cwd=root)
    except RuntimeError as err:
        raise SystemExit(str(err)) from err
    if not branch:
        raise SystemExit("Could not determine the current git branch")
    return branch


def parse_semver_tag(tag: str) -> tuple[int, int, int]:
    match = SEMVER_TAG_RE.fullmatch(tag.strip())
    if not match:
        raise SystemExit(
            f"Tag {tag!r} is not a supported semantic version. Expected forms like 0.9.0 or v0.9.0."
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def normalize_semver_tag(tag: str) -> str:
    major, minor, patch = parse_semver_tag(tag)
    return f"{major}.{minor}.{patch}"


def exact_head_tag(root: Path) -> str | None:
    return try_capture(["git", "describe", "--tags", "--exact-match"], cwd=root)


def latest_reachable_tag(root: Path) -> str | None:
    return try_capture(["git", "describe", "--tags", "--abbrev=0"], cwd=root)


def commits_since_tag(root: Path, tag: str) -> int:
    try:
        return int(run_capture(["git", "rev-list", "--count", f"{tag}..HEAD"], cwd=root))
    except RuntimeError as err:
        raise SystemExit(str(err)) from err


def derive_base_version(root: Path, explicit_base_version: str | None) -> tuple[str, str]:
    if explicit_base_version:
        return explicit_base_version, "CLI override"

    exact_tag = exact_head_tag(root)
    if exact_tag is not None:
        return normalize_semver_tag(exact_tag), f"exact tag {exact_tag}"

    latest_tag = latest_reachable_tag(root)
    if latest_tag is not None:
        major, minor, _ = parse_semver_tag(latest_tag)
        commit_count = commits_since_tag(root, latest_tag)
        return f"{major}.{minor}.{commit_count}", f"{commit_count} commits since {latest_tag}"

    return DEFAULT_BASE_VERSION, f"fallback default {DEFAULT_BASE_VERSION}"


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
    base_version, version_source = derive_base_version(root, args.base_version)
    stamped_version = f"{base_version}-{branch_tag}"
    esphome_bin = args.esphome_bin or default_esphome_bin(root)

    if args.print_version:
        print(stamped_version)
        return 0

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
        print(f"Version source:  {version_source}")
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
