#!/bin/sh
set -eu

MODE="${1:-agy-post-invocation}"
case "$MODE" in
  agy-post-invocation|agy-stop) ;;
  *)
    echo "unsupported manager-history hook mode: $MODE" >&2
    exit 2
    ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

exec python3 "$ROOT/도구/기본/관리자작업기록검사/훅.py" --mode "$MODE"
