#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The DEB is derived from the same fully built portable payload.
exec python3 "${SCRIPT_DIR}/build.py" --format deb "$@"
