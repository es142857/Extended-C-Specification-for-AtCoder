#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
: > "$ROOT/copier.txt"
: > "$ROOT/in.txt"
: > "$ROOT/expected_out.txt"
: > "$ROOT/out.txt"
: > "$ROOT/main.ecpp"
: > "$ROOT/main.cpp"
rm -f -- "$ROOT/main_temporary.cpp"
: > "$ROOT/generator.py"
rm -f -- "$ROOT/a.out"
clear

