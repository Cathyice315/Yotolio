#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/open "$ROOT/Yotolio.app"
