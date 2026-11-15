#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
STATE="$ROOT/.contest"
OJ="$ROOT/.venv/bin/oj"
TEMPORARY="$ROOT/main_temporary.cpp"
trap 'rm -f -- "$TEMPORARY"' EXIT

if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z]$ ]]; then
    echo "Usage: sub <problem letter>" >&2
    exit 2
fi
if [[ ! -s "$STATE" ]]; then
    echo "No contest selected. Run: contest <number|abc123|arc123>" >&2
    exit 1
fi
if [[ ! -x "$OJ" ]]; then
    echo "online-judge-tools is not installed at $OJ" >&2
    exit 1
fi

contest_id=$(tr '[:upper:]' '[:lower:]' < "$STATE")
problem=${1,,}
url="https://atcoder.jp/contests/$contest_id/tasks/${contest_id}_$problem"

cd "$ROOT"
python3 sh/ecpp.py --submit --acl-include preamble.ecpp main.ecpp "$TEMPORARY" >/dev/null
echo "Submitting ${contest_id^^} ${problem^^}: $url"
"$OJ" submit --yes --no-open "$url" "$TEMPORARY"
