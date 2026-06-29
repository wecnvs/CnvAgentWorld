#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

exec python3 "$ROOT/도구/기본/관리자작업기록검사/훅.py" --mode claude-stop
