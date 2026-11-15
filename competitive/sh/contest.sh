#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
STATE="$ROOT/.contest"

if [[ $# -eq 0 ]]; then
    if [[ -s "$STATE" ]]; then
        cat "$STATE"
    else
        echo "No contest selected. Usage: contest <number|abc123|arc123>" >&2
        exit 1
    fi
    exit 0
fi
if [[ $# -ne 1 ]]; then
    echo "Usage: contest <number|abc123|arc123>" >&2
    exit 2
fi

value=${1,,}
if [[ "$value" =~ ^[0-9]+$ ]]; then
    value="abc$value"
fi
if [[ ! "$value" =~ ^(abc|arc|agc)[0-9]+$ ]]; then
    echo "Invalid AtCoder contest: $1" >&2
    exit 2
fi

printf '%s\n' "$value" > "$STATE"
echo "Current contest: ${value^^}"
