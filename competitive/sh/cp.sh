#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
cd "$ROOT"
TEMPORARY="$ROOT/main_temporary.cpp"
trap 'rm -f -- "$TEMPORARY"' EXIT
acl_args=(--acl-include)
if [[ ${1:-} == "-a" ]]; then
    acl_args=()
elif [[ $# -ne 0 ]]; then
    echo "Usage: cp [-a]" >&2
    exit 2
fi
python3 sh/ecpp.py --submit "${acl_args[@]}" preamble.ecpp main.ecpp "$TEMPORARY" >/dev/null
clip.exe < "$TEMPORARY"
echo "Generated and copied submission C++ to the clipboard."
