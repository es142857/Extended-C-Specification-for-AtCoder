#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
cd "$ROOT"
python3 sh/ecpp.py --submit preamble.ecpp main.ecpp main_submit.cpp >/dev/null
clip.exe < main_submit.cpp
echo "Generated and copied main_submit.cpp to the clipboard."

