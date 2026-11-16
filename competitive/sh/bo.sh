#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/soyuz/competitive
MODE=run
EMBED_ACL=false
for argument in "$@"; do
    if [[ "$argument" == "-a" ]]; then
        EMBED_ACL=true
    elif [[ "$MODE" == "run" ]]; then
        MODE=$argument
    else
        echo "Usage: bo [-a] [run|build|translate|term]" >&2
        exit 2
    fi
done
cd "$ROOT"

spinner_pid=""

cleanup_spinner() {
    if [[ -n "$spinner_pid" ]]; then
        kill "$spinner_pid" 2>/dev/null || true
        wait "$spinner_pid" 2>/dev/null || true
        spinner_pid=""
    fi
}

cleanup() {
    cleanup_spinner
    rm -f -- "$ROOT/a.out"
}

trap cleanup EXIT

if [[ ! -s copier.txt ]]; then
    clipboard_file=$(mktemp)
    if powershell.exe -NoProfile -NonInteractive -Command \
        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);[Console]::Write((Get-Clipboard -Raw -Format Text))' \
        >"$clipboard_file" 2>/dev/null; then
        cp -- "$clipboard_file" copier.txt
    else
        echo "bo: clipboard does not contain text; keeping copier.txt empty." >&2
    fi
    rm -f -- "$clipboard_file"
fi

sleep 0.1
python3 sh/copier.py copier.txt in.txt expected_out.txt
python3 sh/ecpp.py --judge preamble.ecpp main.ecpp main.cpp >/dev/null

if [[ "$MODE" == "translate" ]]; then
    exit 0
fi

link_flags=()
if grep -q 'gmpxx.h' main.cpp; then
    link_flags=(-lgmpxx -lgmp)
fi

printf "Compiling"
(
    while true; do
        for spaces in "" " " "  "; do
            printf "\r                              \r"
            printf "\rCompiling%s." "$spaces"
            sleep 0.2
        done
    done
) &
spinner_pid=$!

set +e
g++ -std=gnu++23 -O2 -I "$ROOT/ac-library" main.cpp "${link_flags[@]}" -o a.out
compile_status=$?
set -e
cleanup_spinner

if [[ $compile_status -ne 0 ]]; then
    printf "\rCompile failed.          \n"
    exit "$compile_status"
fi

printf "\rCompiled successfully.    \n"

if [[ "$MODE" == "build" ]]; then
    exit 0
fi

if [[ "$MODE" == "term" ]]; then
    echo "moving on to execution"
    sleep 0.2
    echo "======================="
    ./a.out
    exit 0
fi

if [[ "$MODE" != "run" ]]; then
    echo "Usage: bo [-a] [run|build|translate|term]" >&2
    exit 2
fi

echo "moving on to execution"
sleep 0.2
echo "======================="

: > out.txt
./a.out < in.txt > out.txt

if [[ -f judge.py ]]; then
    if python3 judge.py; then
        if $EMBED_ACL; then
            "$ROOT/sh/cp.sh" -a
        else
            "$ROOT/sh/cp.sh"
        fi
    fi
fi
