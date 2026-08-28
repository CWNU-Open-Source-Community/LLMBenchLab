#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: 'uv' is required. Install uv from https://docs.astral.sh/uv/." >&2
  exit 1
fi

# The macOS PyPy implementation currently rejects the dir_fd/follow_symlinks
# combination used by the bootstrap's no-clobber install. Run this dependency-
# free script with CPython, independently of the backend project environment.
exec uv run \
  --python 'cpython>=3.11' \
  --script "$project_root/scripts/ensure_credential_keys.py" \
  "$@"
