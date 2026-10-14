#!/usr/bin/env python3
"""Split combined input and expected-output blocks from copier.txt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SEPARATOR = "=========="


def split_blocks(text: str) -> list[str]:
    blocks: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.strip() == SEPARATOR:
            blocks.append([])
        else:
            blocks[-1].append(line)

    while blocks and not any(line.strip() for line in blocks[-1]):
        blocks.pop()
    return ["\n".join(block).strip("\n") for block in blocks]


def expand(source: Path, input_file: Path, expected_file: Path) -> None:
    text = source.read_text(encoding="utf-8")
    if not text.strip():
        return

    blocks = split_blocks(text)
    input_block = blocks[0]
    outputs = blocks[1:]

    input_text = input_block
    if input_text and not input_text.endswith("\n"):
        input_text += "\n"

    expected_parts: list[str] = []
    if outputs:
        expected_parts.append(SEPARATOR)
    for output in outputs:
        if output:
            expected_parts.append(output)
        expected_parts.append(SEPARATOR)
    expected_text = "\n".join(expected_parts)
    if expected_text:
        expected_text += "\n"

    input_file.write_text(input_text, encoding="utf-8")
    expected_file.write_text(expected_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("copier.txt"))
    parser.add_argument("input", nargs="?", type=Path, default=Path("in.txt"))
    parser.add_argument(
        "expected", nargs="?", type=Path, default=Path("expected_out.txt")
    )
    args = parser.parse_args()
    try:
        expand(args.source, args.input, args.expected)
    except (OSError, ValueError) as error:
        print(f"copier: error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
