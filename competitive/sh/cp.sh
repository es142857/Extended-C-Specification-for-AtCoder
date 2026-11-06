#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
cd "$ROOT"
TEMPORARY="$ROOT/main_temporary.cpp"
trap 'rm -f -- "$TEMPORARY"' EXIT
python3 sh/ecpp.py --submit preamble.ecpp main.ecpp "$TEMPORARY" >/dev/null
clip.exe < "$TEMPORARY"
echo "Generated and copied submission C++ to the clipboard."


