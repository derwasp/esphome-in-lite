#!/usr/bin/env sh

# Activate the repo-local virtualenv in the current shell.
# Usage:
#   source ./activate.sh

_activate_is_sourced=0

if [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in
    *:file) _activate_is_sourced=1 ;;
  esac
elif [ -n "${BASH_VERSION:-}" ]; then
  if [ "${BASH_SOURCE[0]:-}" != "$0" ]; then
    _activate_is_sourced=1
  fi
fi

if [ "$_activate_is_sourced" -ne 1 ]; then
  echo "This script must be sourced to affect the current terminal." >&2
  echo "Run: source ./activate.sh" >&2
  exit 1
fi

if [ -n "${ZSH_VERSION:-}" ]; then
  _activate_script_path="${(%):-%N}"
elif [ -n "${BASH_VERSION:-}" ]; then
  _activate_script_path="${BASH_SOURCE[0]}"
else
  _activate_script_path="$0"
fi

_activate_repo_root="$(CDPATH= cd -- "$(dirname -- "$_activate_script_path")" && pwd)"
_activate_venv_script="${_activate_repo_root}/.venv/bin/activate"

if [ ! -f "$_activate_venv_script" ]; then
  echo "Virtualenv activation script not found: $_activate_venv_script" >&2
  echo "Create it first with: python3 -m venv .venv" >&2
  return 1
fi

. "$_activate_venv_script"

unset _activate_is_sourced
unset _activate_script_path
unset _activate_repo_root
unset _activate_venv_script
