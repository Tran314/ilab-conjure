#!/usr/bin/env sh
set -eu
BUNDLE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$BUNDLE_DIR/ilab-conjure-launcher" "$@"
