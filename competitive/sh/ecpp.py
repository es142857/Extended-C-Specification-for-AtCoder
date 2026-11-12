#!/usr/bin/env python3
"""Extended C++ (.ecpp) to C++ source translator."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DECLARATION = re.compile(
    r"(?m)^(?P<indent>[ \t]*)"
    r"(?P<template>template[ \t]*<[^>\n]*>[ \t]*)?"
    r"(?P<return>(?!(?:return|co_return|if|for|while|switch)\b)"
    r"[A-Za-z_~][\w:<>,*& \t]*?)"
    r"[ \t]+(?:(?P<marker>additional)[ \t]+)?"
    r"(?P<receiver>[A-Za-z_][\w:<>,*& \t]*?)"
    r"[ \t]*(?P<dot>\.)[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*"
    r"\((?P<arguments>[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
    r"(?P<const>[ \t]+const)?(?=[ \t\r\n]*\{)"
)


class TranslationError(RuntimeError):
    pass


def code_mask(source: str) -> list[bool]:
    """Return a mask that is false inside comments and string/char literals."""
    mask = [True] * len(source)
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            end = source.find("\n", i)
            if end == -1:
                end = len(source)
            for j in range(i, end):
                mask[j] = False
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) if end == -1 else end + 2
            for j in range(i, end):
                mask[j] = False
            i = end
        elif source[i] in {'"', "'"}:
            quote = source[i]
            mask[i] = False
            i += 1
            while i < len(source):
                mask[i] = False
                if source[i] == "\\":
                    if i + 1 < len(source):
                        mask[i + 1] = False
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            i += 1
    return mask


def translate_line_continuations(source: str) -> str:
    """Join code lines ending in `\\` while preserving later line numbers."""
    mask = code_mask(source)
    output: list[str] = []
    pending = ""
    removed_newlines: list[str] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        body, newline = line, ""
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]
        positions = [
            index
            for index, character in enumerate(body)
            if mask[offset + index] and not character.isspace()
        ]
        if newline and positions and body[positions[-1]] == "\\":
            slash = positions[-1]
            pending += body[:slash] + body[slash + 1 :]
            removed_newlines.append(newline)
        else:
            output.append(pending + body + newline)
            output.extend(removed_newlines)
            pending = ""
            removed_newlines.clear()
        offset += len(line)
    if pending:
        output.append(pending)
        output.extend(removed_newlines)
    return "".join(output)


def embed_atcoder_library(source: str, library_root: Path) -> str:
    """Recursively inline the complete bundled `atcoder/all` dependency tree."""
    include = re.compile(
        r'^[ \t]*#include[ \t]*["<](atcoder/[a-z_]+(?:\.hpp)?)[">][ \t]*$'
    )
    guard = re.compile(r"^[ \t]*#.*\bATCODER_[A-Z0-9_]+_HPP\b")
    standard_include = re.compile(r"^[ \t]*#include[ \t]*<[^>]+>[ \t]*$")
    seen: set[Path] = set()

    def specialize(lines: list[str]) -> list[str]:
        """Select the GNU C++23 ACL branches so no ACL directives remain."""
        output: list[str] = []
        stack: list[tuple[bool, bool]] = []
        active = True
        for line in lines:
            directive = line.strip()
            if directive in {"#ifdef _MSC_VER", "#ifndef _MSC_VER"}:
                condition = directive.startswith("#ifndef")
                stack.append((active, condition))
                active = active and condition
            elif re.fullmatch(r"#if __cplusplus >= (?:201703|202002)L", directive):
                stack.append((active, True))
            elif directive == "#else":
                if not stack:
                    raise TranslationError("unmatched #else in bundled ACL")
                parent, condition = stack[-1]
                active = parent and not condition
            elif directive == "#endif":
                if not stack:
                    raise TranslationError("unmatched #endif in bundled ACL")
                parent, _ = stack.pop()
                active = parent
            elif active:
                output.append(line)
        if stack:
            raise TranslationError("unterminated conditional in bundled ACL")
        return output

    def expand(name: str) -> list[str]:
        path = library_root / name
        if path in seen:
            return []
        if not path.is_file():
            raise TranslationError(f"bundled ACL header not found: {name}")
        seen.add(path)
        result: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            original_line = line
            line = line.split("//", 1)[0].rstrip()
            nested = include.fullmatch(line)
            if nested:
                result.extend(expand(nested.group(1)))
            elif (
                guard.match(original_line)
                or standard_include.match(line)
                or line.strip() == "#pragma once"
                or line.lstrip().startswith("//")
            ):
                continue
            else:
                result.append(line)
        return result

    output: list[str] = []
    replaced = False
    for line in source.splitlines():
        match = include.fullmatch(line)
        if match and match.group(1) == "atcoder/all":
            output.extend(specialize(expand(match.group(1))))
            replaced = True
        else:
            output.append(line)
    if not replaced:
        raise TranslationError("preamble.ecpp must include <atcoder/all>")
    return "\n".join(output) + "\n"


def extract_embedded_acl(source: str) -> tuple[str, str | None]:
    """Remove and return the raw minified ACL block stored in preamble.ecpp."""
    block = re.compile(
        r"(?ms)^//__ACL_BEGIN__\r?\n(?P<code>.*?)^//__ACL_END__\r?\n?"
    )
    match = block.search(source)
    if not match:
        return source, None
    return (
        source[: match.start()] + "//__ACL_SLOT__\n" + source[match.end() :],
        match.group("code").strip(),
    )


def inject_embedded_acl(source: str, acl: str) -> str:
    if "//__ACL_SLOT__" in source:
        return source.replace("//__ACL_SLOT__", acl, 1)
    source, count = re.subn(
        r"(?m)^(#include<bits/stdc\+\+\.h>[ \t]*\r?\n)",
        lambda item: item.group(1) + acl + "\n",
        source,
        count=1,
    )
    if count != 1:
        raise TranslationError("could not place embedded ACL")
    return source


def translate_declarations(source: str) -> tuple[str, set[str]]:
    mask = code_mask(source)
    matches = [
        match
        for match in DECLARATION.finditer(source)
        if mask[match.start("return")] and mask[match.start("dot")]
    ]

    names: set[str] = set()
    for match in reversed(matches):
        name = match.group("name")
        names.add(name)
        if match.group("marker"):
            return_type = match.group("return").strip()
            receiver = match.group("receiver").strip()
        else:
            signature = source[match.start("return") : match.start("dot")]
            angle_depth = round_depth = square_depth = 0
            boundaries = []
            index = 0
            while index < len(signature):
                character = signature[index]
                if character == "<":
                    angle_depth += 1
                elif character == ">" and angle_depth:
                    angle_depth -= 1
                elif character == "(":
                    round_depth += 1
                elif character == ")" and round_depth:
                    round_depth -= 1
                elif character == "[":
                    square_depth += 1
                elif character == "]" and square_depth:
                    square_depth -= 1
                elif (
                    character.isspace()
                    and angle_depth == round_depth == square_depth == 0
                ):
                    end = index + 1
                    while end < len(signature) and signature[end].isspace():
                        end += 1
                    boundaries.append((index, end))
                    index = end - 1
                index += 1
            if not boundaries:
                continue
            boundary_start, boundary_end = boundaries[-1]
            return_type = signature[:boundary_start].strip()
            receiver = signature[boundary_end:].strip()
        arguments = match.group("arguments").strip()
        is_const = bool(match.group("const"))
        self_type = f"const {receiver}&" if is_const else f"{receiver}&"
        parameters = f"{self_type} self"
        if arguments and arguments != "void":
            parameters += f", {arguments}"
        replacement = (
            f"{match.group('indent')}{match.group('template') or ''}{return_type} "
            f"__x_{name}({parameters})"
        )
        source = source[: match.start()] + replacement + source[match.end() :]
    return source, names


def find_closing_paren(source: str, opening: int, mask: list[bool]) -> int:
    depth = 0
    for i in range(opening, len(source)):
        if not mask[i]:
            continue
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise TranslationError(f"unclosed argument list at character {opening}")


def receiver_start(source: str, dot: int, mask: list[bool]) -> int:
    i = dot - 1
    while i >= 0 and source[i] in " \t":
        i -= 1
    end = i
    paren = bracket = 0
    stops = set(";{}=,+-*/%!&|^?<>\n")
    while i >= 0:
        if not mask[i]:
            # String and character literals are indivisible parts of the
            # receiver (`str("abc").index('a')`, `"abc".index('a')`).
            # Skip their whole masked span while still stopping at comments.
            span_start = i
            while span_start > 0 and not mask[span_start - 1]:
                span_start -= 1
            span = source[span_start : i + 1]
            if span.startswith("//") or span.startswith("/*"):
                break
            i = span_start - 1
            continue
        ch = source[i]
        if ch == ")":
            # A loop/control macro can be immediately followed by its body,
            # e.g. `rep(_, n)values.append(x)`.  The closing parenthesis is
            # not part of the member-call receiver in that form.
            if (
                paren == 0
                and bracket == 0
                and i + 1 < dot
                and (source[i + 1].isalnum() or source[i + 1] == "_")
            ):
                break
            paren += 1
        elif ch == "(":
            if paren == 0:
                break
            paren -= 1
        elif ch == "]":
            bracket += 1
        elif ch == "[":
            if bracket == 0:
                break
            bracket -= 1
        elif paren == 0 and bracket == 0:
            if ch.isspace() or ch in stops:
                break
        i -= 1
    start = i + 1
    if start > end:
        raise TranslationError(f"missing receiver before character {dot}")
    return start


def implicit_placeholder_depth(source, target, candidates, mask) -> int:
    """Return the implicit `th` depth for a nested map/filter call."""
    depth = 1
    for parent in candidates:
        if parent.start() >= target.start():
            continue
        if parent.group("name") not in {"map", "filter"}:
            continue
        opening = parent.end() - 1
        closing = find_closing_paren(source, opening, mask)
        if not opening < target.start() < closing:
            continue
        arguments = source[opening + 1 : closing].strip()
        if arguments.lstrip().startswith("["):
            continue
        depth += 1
    return depth


def translate_function_map_calls(source: str) -> str:
    """Translate `map(function, range)` into the internal member dispatch form."""
    call = re.compile(r"(?<![\w.:>])map[ \t]*\(")

    while True:
        mask = code_mask(source)
        candidates = [match for match in call.finditer(source) if mask[match.start()]]
        transformed = False

        for match in reversed(candidates):
            opening = match.end() - 1
            closing = find_closing_paren(source, opening, mask)
            argument_text = source[opening + 1 : closing]
            argument_mask = code_mask(argument_text)
            masked_arguments = "".join(
                character if argument_mask[index] else " "
                for index, character in enumerate(argument_text)
            )
            arguments = split_top_level_commas(masked_arguments)

            if len(arguments) == 2:
                comma = len(arguments[0])
            else:
                # A comparison such as `th < 3` has no closing `>`, so the
                # generic comma splitter may mistake it for a template list.
                # With paired delimiters still respected, its sole top-level
                # comma is unambiguously the function/range separator.
                round_depth = square_depth = brace_depth = 0
                commas: list[int] = []
                for index, character in enumerate(masked_arguments):
                    if character == "(":
                        round_depth += 1
                    elif character == ")" and round_depth:
                        round_depth -= 1
                    elif character == "[":
                        square_depth += 1
                    elif character == "]" and square_depth:
                        square_depth -= 1
                    elif character == "{":
                        brace_depth += 1
                    elif character == "}" and brace_depth:
                        brace_depth -= 1
                    elif (
                        character == ","
                        and round_depth == square_depth == brace_depth == 0
                    ):
                        commas.append(index)
                if len(commas) != 1:
                    continue
                comma = commas[0]

            function = argument_text[:comma].strip()
            receiver = argument_text[comma + 1 :].strip()
            if not function or not receiver:
                continue
            function = {"int": "stoi", "ll": "stoll"}.get(function, function)

            replacement = f"({receiver}).map({function})"
            source = source[: match.start()] + replacement + source[closing + 1 :]
            transformed = True
            break

        if not transformed:
            return source


def translate_while_conditions(source: str) -> str:
    """Use container nonemptiness for direct `while(value)` conditions."""
    pattern = re.compile(r"\bwhile[ \t]*\(")
    while True:
        mask = code_mask(source)
        matches = [match for match in pattern.finditer(source) if mask[match.start()]]
        transformed = False
        for match in reversed(matches):
            opening = match.end() - 1
            closing = find_closing_paren(source, opening, mask)
            condition = source[opening + 1 : closing].strip()
            if not condition or condition.startswith("__W("):
                continue
            # A C++ condition declaration cannot be passed as an expression.
            if re.match(
                r"^(?:auto|bool|char|short|int|long|float|double|str|ll|ld|"
                r"[A-Za-z_]\w*(?:\s*<[^;{}]+>)?)\s+[A-Za-z_]\w*\s*=",
                condition,
            ):
                continue
            source = source[: opening + 1] + f"__W(({condition}))" + source[closing:]
            transformed = True
            break
        if not transformed:
            return source


def translate_calls(source: str, names: set[str]) -> str:
    if not names:
        return source
    method = re.compile(
        r"\.[ \t]*(?P<name>" + "|".join(map(re.escape, sorted(names))) + r")"
        r"[ \t]*\("
    )
    while True:
        mask = code_mask(source)
        candidates = [m for m in method.finditer(source) if mask[m.start()]]
        if not candidates:
            return source
        match = candidates[-1]
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        start = receiver_start(source, match.start(), mask)
        receiver = source[start : match.start()].strip()
        arguments = source[opening + 1 : closing].strip()
        name = match.group("name")
        if name == "map" and arguments.startswith("{") and arguments.endswith("}"):
            elements = split_top_level_commas(arguments[1:-1])
            if len(elements) == 1:
                arguments = elements[0].strip()
            elif len(elements) == 2:
                arguments = f"pair{{{arguments[1:-1]}}}"
            elif len(elements) > 2:
                arguments = f"tuple{{{arguments[1:-1]}}}"
        forwarded_receiver = (
            "std::forward<decltype(__r)>(__r)"
        )
        if (
            name in {"add", "append", "appendleft"}
            and arguments.startswith("{")
            and arguments.endswith("}")
        ):
            arguments = (
                "typename std::remove_cvref_t<decltype(__r)>"
                f"::value_type{arguments}"
            )
        call_args = (
            forwarded_receiver
            if not arguments
            else f"{forwarded_receiver}, {arguments}"
        )
        # In a native member call, the receiver is evaluated before the
        # arguments.  A direct rewrite to a free function would lose that
        # guarantee because free-function argument order is unspecified.
        if name in {"map", "filter"} and arguments:
            argument_mask = code_mask(arguments)
            argument_is_lambda = arguments.lstrip().startswith("[")
            placeholder_depth = implicit_placeholder_depth(
                source, match, candidates, mask
            )
            placeholder_name = (
                "th" if placeholder_depth == 1 else f"th{placeholder_depth}"
            )
            bare_map_function = (
                name == "map"
                and re.fullmatch(r"[A-Za-z_]\w*", arguments) is not None
                and re.fullmatch(r"th\d*", arguments) is None
            )
            if bare_map_function:
                arguments = f"{arguments}({placeholder_name})"
                argument_mask = code_mask(arguments)
            uses_element_placeholder = not argument_is_lambda and any(
                argument_mask[placeholder.start()]
                for placeholder in re.finditer(
                    rf"(?<![\w.:>]){re.escape(placeholder_name)}\b",
                    arguments,
                )
            )
            zero_argument_lambda = bool(
                re.match(
                    r"^[ \t]*\[[^\]\n]*\][ \t]*(?:\([ \t]*\))?[ \t]*\{",
                    arguments,
                )
            )
            if uses_element_placeholder:
                fallback = (
                    "[&](auto&& __v) -> decltype(auto) { "
                    f"auto&& {placeholder_name} = "
                    "std::forward<decltype(__v)>("
                    "__v); return "
                    f"({arguments}); }}"
                )
            elif zero_argument_lambda:
                fallback = (
                    "[&](auto&& __v) -> decltype(auto) { "
                    "(void)__v; return "
                    f"({arguments})(); }}"
                )
            else:
                fallback = (
                    "[&](auto&& __v) -> decltype(auto) { "
                    "if constexpr (requires { "
                    f"({arguments})(std::forward<decltype(__v)>("
                    "__v)); }) { return "
                    f"({arguments})(std::forward<decltype(__v)>("
                    "__v)); } else { return "
                    f"({arguments}); }} }}"
                )
            if uses_element_placeholder:
                replacement = (
                    "([&](auto&& __r) -> decltype(auto) { return "
                    f"__d_{name}("
                    f"{forwarded_receiver}, {fallback}); "
                    f"}})({receiver})"
                )
            else:
                replacement = (
                    "([&](auto&& __r) -> decltype(auto) { "
                    f"if constexpr (requires {{ __x_{name}("
                    f"__r, {arguments}); }}) {{ "
                    f"return __d_{name}({call_args}); "
                    f"}} else {{ return __d_{name}("
                    f"{forwarded_receiver}, {fallback}); }} "
                    f"}})({receiver})"
                )
        else:
            replacement = (
                "([&](auto&& __r) -> decltype(auto) { return "
                f"__d_{name}({call_args}); "
                f"}})({receiver})"
            )
        source = source[:start] + replacement + source[closing + 1 :]


def translate_casts(source: str) -> str:
    """Translate Extended C++ functional casts to explicit C-style casts."""
    cast = re.compile(r"\b(?P<type>int|str|bool|ld|ll)[ \t]*\(")
    mask = code_mask(source)
    matches = [match for match in cast.finditer(source) if mask[match.start()]]
    for match in reversed(matches):
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        if not source[opening + 1 : closing].strip():
            continue
        replacement = (
            "__L("
            if match.group("type") == "ll"
            else f"({match.group('type')})("
        )
        source = source[: match.start()] + replacement + source[match.end() :]
    return source


def translate_string_literals(source: str) -> str:
    """Give ordinary double-quoted literals the ECPP `str` value type."""
    output: list[str] = []
    index = 0
    line_start = True
    while index < len(source):
        if line_start:
            probe = index
            while probe < len(source) and source[probe] in " \t":
                probe += 1
            if probe < len(source) and source[probe] == "#":
                end = source.find("\n", probe)
                end = len(source) if end == -1 else end + 1
                output.append(source[index:end])
                index = end
                line_start = True
                continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = len(source) if end == -1 else end + 1
            output.append(source[index:end])
            index = end
            line_start = True
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end == -1 else end + 2
            block = source[index:end]
            output.append(block)
            index = end
            line_start = block.endswith("\n")
            continue
        if source[index] == "'":
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                elif source[end] == "'":
                    end += 1
                    break
                else:
                    end += 1
            output.append(source[index:end])
            index = end
            line_start = False
            continue
        if source[index] == '"':
            literal_prefix = re.search(r"(?:u8|[uULR])$", source[:index])
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                elif source[end] == '"':
                    end += 1
                    break
                else:
                    end += 1
            literal = source[index:end]
            if literal_prefix:
                output.append(literal)
                index = end
                line_start = False
                continue
            # Preserve C++'s adjacent-literal concatenation inside one str.
            chain_end = end
            while True:
                next_start = chain_end
                while next_start < len(source) and source[next_start].isspace():
                    next_start += 1
                if next_start >= len(source) or source[next_start] != '"':
                    break
                next_end = next_start + 1
                while next_end < len(source):
                    if source[next_end] == "\\":
                        next_end += 2
                    elif source[next_end] == '"':
                        next_end += 1
                        break
                    else:
                        next_end += 1
                chain_end = next_end
            literal = source[index:chain_end]
            prefix = "".join(output)
            already_str = re.search(r"\bstr[ \t]*\([ \t]*$", prefix) is not None
            output.append(literal if already_str else f"(str({literal}))")
            index = chain_end
            line_start = False
            continue
        character = source[index]
        output.append(character)
        index += 1
        line_start = character in "\r\n"
    return "".join(output)


def translate_pair_member_aliases(source: str) -> str:
    """Translate pair `.f` and `.s` aliases to `.first` and `.second`."""
    mask = code_mask(source)
    alias = re.compile(r"\.[ \t]*(?P<name>f|s)\b")
    replacements = []
    for match in alias.finditer(source):
        if not mask[match.start()]:
            continue
        following = match.end()
        while following < len(source) and source[following] in " \t":
            following += 1
        if following < len(source) and source[following] == "(":
            continue
        start = receiver_start(source, match.start(), mask)
        receiver = source[start : match.start()].strip()
        if re.fullmatch(
            r"(?:0[xX][0-9A-Fa-f']+|0[bB][01']+|[0-9][0-9']*)",
            receiver,
        ):
            continue
        member = "first" if match.group("name") == "f" else "second"
        replacements.append((match.start(), match.end(), f".{member}"))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_inferred_pairs(source: str) -> str:
    """Turn two-element braced expressions such as `{a,b}` into `pair{a,b}`."""
    while True:
        mask = code_mask(source)
        stack: list[int] = []
        candidate: tuple[int, int] | None = None
        for index, character in enumerate(source):
            if not mask[index]:
                continue
            if character == "{":
                stack.append(index)
            elif character == "}" and stack:
                opening = stack.pop()
                elements = split_top_level_commas(source[opening + 1 : index])
                if len(elements) != 2 or any(not part.strip() for part in elements):
                    continue
                previous = opening - 1
                while previous >= 0 and source[previous].isspace():
                    previous -= 1
                # An identifier, closing delimiter, or colon before `{` denotes
                # an explicit initializer/body/range rather than an inferred pair.
                if previous >= 0 and (
                    source[previous].isalnum()
                    or source[previous] in "_)]}>:"
                ):
                    continue
                candidate = (opening, index)
                break
        if candidate is None:
            return source
        opening, closing = candidate
        source = source[:opening] + "pair" + source[opening:closing + 1] + source[closing + 1:]


def find_closing_bracket(source: str, opening: int, mask: list[bool]) -> int:
    depth = 0
    for index in range(opening, len(source)):
        if not mask[index]:
            continue
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                return index
    raise TranslationError(f"unclosed subscript at character {opening}")


def find_closing_brace(source: str, opening: int, mask: list[bool]) -> int:
    depth = 0
    for index in range(opening, len(source)):
        if not mask[index]:
            continue
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise TranslationError(f"unclosed lambda body at character {opening}")


def slice_receiver_start(source: str, opening: int, mask: list[bool]) -> int:
    """Find the expression immediately before a slice subscript."""
    index = opening - 1
    while index >= 0 and source[index] in " \t":
        index -= 1
    end = index
    round_depth = square_depth = brace_depth = angle_depth = 0
    stops = set(";=,+-*/%!&|^?\n")

    while index >= 0:
        if not mask[index]:
            break
        character = source[index]
        if character == ")":
            round_depth += 1
        elif character == "(":
            if round_depth == 0:
                break
            round_depth -= 1
        elif character == "]":
            square_depth += 1
        elif character == "[":
            if square_depth == 0:
                break
            square_depth -= 1
        elif character == "}":
            brace_depth += 1
        elif character == "{":
            if brace_depth == 0:
                break
            brace_depth -= 1
        elif character == ">":
            angle_depth += 1
        elif character == "<":
            if angle_depth == 0:
                break
            angle_depth -= 1
        elif (
            round_depth == square_depth == brace_depth == angle_depth == 0
            and (character.isspace() or character in stops)
        ):
            break
        index -= 1

    start = index + 1
    if start > end:
        raise TranslationError(f"missing slice receiver before character {opening}")
    return start


def slice_colons(text: str) -> list[int]:
    """Return top-level slice colons, excluding `?:` and qualified names."""
    mask = code_mask(text)
    scope_colons: set[int] = set()
    for match in re.finditer(r"::", text):
        if not mask[match.start()] or not mask[match.start() + 1]:
            continue
        before = match.start() - 1
        while before >= 0 and text[before] in " \t":
            before -= 1
        after = match.end()
        while after < len(text) and text[after] in " \t":
            after += 1
        prefix = re.search(r"([A-Za-z_]\w*)[ \t]*$", text[: match.start()])
        suffix = re.match(r"([A-Za-z_]\w*)", text[after:])
        suffix_end = after + (suffix.end() if suffix else 0)
        while suffix_end < len(text) and text[suffix_end] in " \t":
            suffix_end += 1
        is_qualified_name = bool(
            prefix
            and suffix
            and (
                prefix.group(1) in {"std", "atcoder"}
                or suffix_end < len(text) and text[suffix_end] == "("
            )
        ) or (before >= 0 and text[before] == ">" and suffix is not None)
        if is_qualified_name:
            scope_colons.update({match.start(), match.start() + 1})

    round_depth = square_depth = brace_depth = 0
    ternary_depth = 0
    colons: list[int] = []
    for index, character in enumerate(text):
        if not mask[index]:
            continue
        if character == "(":
            round_depth += 1
        elif character == ")" and round_depth:
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]" and square_depth:
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
        elif round_depth == square_depth == brace_depth == 0:
            if character == "?":
                ternary_depth += 1
            elif character == ":" and index not in scope_colons:
                if ternary_depth:
                    ternary_depth -= 1
                else:
                    colons.append(index)
    return colons


def translate_slice_expressions(source: str) -> str:
    """Translate Python-style vector slices into `__S` calls."""
    while True:
        mask = code_mask(source)
        candidate: tuple[int, int, list[int]] | None = None
        for opening, character in enumerate(source):
            if character != "[" or not mask[opening]:
                continue
            closing = find_closing_bracket(source, opening, mask)
            content = source[opening + 1 : closing]
            colons = slice_colons(content)
            if colons:
                candidate = (opening, closing, colons)
                break
        if candidate is None:
            return source

        opening, closing, colons = candidate
        if len(colons) > 2:
            raise TranslationError(
                f"slice at character {opening} has more than two colons"
            )
        content = source[opening + 1 : closing]
        boundaries = [-1, *colons, len(content)]
        parts = [
            content[boundaries[index] + 1 : boundaries[index + 1]].strip()
            for index in range(len(boundaries) - 1)
        ]
        if len(parts) == 2:
            parts.append("")
        start_expression, stop_expression, step_expression = parts
        start = (
            "std::nullopt"
            if not start_expression
            else f"std::optional<ll>{{(ll)({start_expression})}}"
        )
        stop = (
            "std::nullopt"
            if not stop_expression
            else f"std::optional<ll>{{(ll)({stop_expression})}}"
        )
        step = "1" if not step_expression else f"(ll)({step_expression})"
        receiver_start = slice_receiver_start(source, opening, mask)
        receiver = source[receiver_start:opening].strip()
        replacement = f"__S({receiver}, {start}, {stop}, {step})"
        source = source[:receiver_start] + replacement + source[closing + 1 :]


def walrus_rhs_end(source: str, start: int, mask: list[bool]) -> int:
    """Find the delimiter ending a walrus right-hand side on one line."""
    round_depth = square_depth = brace_depth = angle_depth = 0
    for index in range(start, len(source)):
        if not mask[index]:
            continue
        character = source[index]
        if character == "(":
            round_depth += 1
        elif character == ")":
            if round_depth == 0:
                return index
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]":
            if square_depth == 0:
                return index
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}":
            if brace_depth == 0:
                return index
            brace_depth -= 1
        elif character == "<":
            angle_depth += 1
        elif character == ">" and angle_depth:
            angle_depth -= 1
        elif (
            character in ",;"
            and round_depth == square_depth == brace_depth == angle_depth == 0
        ):
            return index
    return len(source)


def translate_walrus_expressions(source: str) -> str:
    """Translate declaring-name and assignable-lvalue walrus expressions."""
    walrus = re.compile(r":=[ \t]*")
    output: list[str] = []
    logical_line = 1

    for line in source.splitlines(keepends=True):
        directive = re.match(r'^[ \t]*#line[ \t]+(\d+)[ \t]+"main\.ecpp"', line)
        if directive:
            logical_line = int(directive.group(1))
            output.append(line)
            continue
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            body, newline = line[:-1], line[-1]
        else:
            body, newline = line, ""

        assignments: list[tuple[str, str, bool, bool]] = []
        while True:
            mask = code_mask(body)
            matches = [match for match in walrus.finditer(body) if mask[match.start()]]
            if not matches:
                break

            candidates: list[tuple[re.Match[str], int, int, str]] = []
            for match in matches:
                start = receiver_start(body, match.start(), mask)
                target = body[start : match.start()].strip()
                if not target:
                    raise TranslationError(
                        f"missing target before walrus at character {match.start()}"
                    )
                end = walrus_rhs_end(body, match.end(), mask)
                nested = any(
                    match.end() <= other.start() < end
                    for other in matches
                    if other is not match
                )
                if not nested:
                    candidates.append((match, end, start, target))
            if not candidates:
                raise TranslationError("could not determine walrus expression nesting")

            match, end, start, target = candidates[0]
            expression = body[match.end() : end].strip()
            if not expression:
                raise TranslationError(
                    f"missing expression after walrus assignment to {target}"
                )
            if re.fullmatch(r"[A-Za-z_]\w*", target):
                inline_input = re.fullmatch(
                    r"input[ \t]*\([ \t]*\)", expression
                ) is not None
                loop_end = leading_loop_prefix_end(body)
                assignments.append((target, expression, inline_input, start >= loop_end > 0))
                replacement = (
                    f"({target} = str({expression}))" if inline_input else target
                )
                body = body[:start] + replacement + body[end:]
            else:
                body = body[:start] + f"({target} = {expression})" + body[end:]

        if assignments:
            indent = re.match(r"[ \t]*", body).group()
            for name, expression, inline_input, local in assignments:
                if local:
                    continue
                declaration = f"str {name}" if inline_input else f"{name} = {expression}"
                output.append(
                    f'#line {logical_line} "main.ecpp"\n'
                    f"{indent}{declaration}{newline or chr(10)}"
                )
            local_assignments = [assignment for assignment in assignments if assignment[3]]
            if local_assignments:
                loop_end = leading_loop_prefix_end(body)
                declarations = "".join(
                    f"{'str ' if inline_input else 'auto '}{name}"
                    f"{'' if inline_input else f'={expression}'};"
                    for name, expression, inline_input, _ in local_assignments
                )
                body = body[:loop_end] + "{" + declarations + body[loop_end:] + ";}"
            output.append(f'#line {logical_line} "main.ecpp"\n')
        output.append(body + newline)
        logical_line += 1

    return "".join(output)


def leading_loop_prefix_end(source: str) -> int:
    """Return the end of chained loop headers at the start of a statement."""
    mask = code_mask(source)
    position = len(source) - len(source.lstrip(" \t"))
    loop_names = {"for", "rep", "rrep", "range", "rrange", "srange", "rsrange", "subset"}
    found = False
    while position < len(source):
        match = re.match(r"[A-Za-z_]\w*", source[position:])
        if not match or match.group() not in loop_names:
            break
        opening = position + match.end()
        while opening < len(source) and source[opening] in " \t":
            opening += 1
        if opening >= len(source) or source[opening] != "(" or not mask[opening]:
            break
        closing = find_closing_paren(source, opening, mask)
        position = closing + 1
        while position < len(source) and source[position] in " \t":
            position += 1
        found = True
    return position if found else 0


def translate_chained_assignments(source: str) -> str:
    """Expand leading `a = b = value` assignments from right to left."""
    comments = comment_mask(source)
    chain = re.compile(
        r"^(?P<indent>[ \t]*)"
        r"(?P<assignments>(?:[A-Za-z_]\w*[ \t]*=(?!=)[ \t]*){2,})"
        r"(?P<value>.+?)$"
    )
    output: list[str] = []
    offset = 0
    logical_line = 1

    for line in source.splitlines(keepends=True):
        directive = re.match(r'^[ \t]*#line[ \t]+(\d+)[ \t]+"main\.ecpp"', line)
        if directive:
            logical_line = int(directive.group(1))
            output.append(line)
            offset += len(line)
            continue
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            body, newline = line[:-1], line[-1]
        else:
            body, newline = line, ""

        comment_at = len(body)
        for index in range(len(body) - 1):
            if body[index : index + 2] == "//" and comments[offset + index]:
                comment_at = index
                break
        statement = body[:comment_at]
        comment = body[comment_at:]
        trailing = statement[len(statement.rstrip()) :]
        statement = statement.rstrip()
        explicit_semicolon = statement.endswith(";")
        if explicit_semicolon:
            statement = statement[:-1].rstrip()

        statement_mask = code_mask(statement)
        masked_statement = "".join(
            character if statement_mask[index] else " "
            for index, character in enumerate(statement)
        )
        match = chain.match(masked_statement)
        if not match:
            output.append(line)
            offset += len(line)
            continue

        names = re.findall(
            r"([A-Za-z_]\w*)[ \t]*=(?!=)",
            statement[match.start("assignments") : match.end("assignments")],
        )
        value_start = match.start("value")
        round_depth = square_depth = brace_depth = angle_depth = 0
        comma_at: int | None = None
        for index in range(value_start, len(statement)):
            if not statement_mask[index]:
                continue
            character = statement[index]
            if character == "(":
                round_depth += 1
            elif character == ")" and round_depth:
                round_depth -= 1
            elif character == "[":
                square_depth += 1
            elif character == "]" and square_depth:
                square_depth -= 1
            elif character == "{":
                brace_depth += 1
            elif character == "}" and brace_depth:
                brace_depth -= 1
            elif character == "<":
                angle_depth += 1
            elif character == ">" and angle_depth:
                angle_depth -= 1
            elif (
                character == ","
                and round_depth == square_depth == brace_depth == angle_depth == 0
            ):
                comma_at = index
                break

        value_end = comma_at if comma_at is not None else match.end("value")
        value = statement[value_start:value_end].strip()
        comma_tail = (
            statement[comma_at + 1 :].strip() if comma_at is not None else ""
        )
        if len(names) < 2 or not value:
            output.append(line)
            offset += len(line)
            continue

        indent = match.group("indent")
        generated: list[str] = [f"{indent}{names[-1]} = {value}"]
        for index in range(len(names) - 2, -1, -1):
            generated.append(f"{indent}{names[index]} = {names[index + 1]}")
        if comma_tail:
            generated.append(f"{indent}{comma_tail}")

        for generated_index, assignment in enumerate(generated):
            is_last = generated_index == len(generated) - 1
            suffix = ";"
            output.append(f'#line {logical_line} "main.ecpp"\n')
            if is_last:
                output.append(assignment + suffix + trailing + comment + newline)
            else:
                output.append(assignment + suffix + (newline or "\n"))
        offset += len(line)
        logical_line += 1

    return "".join(output)


def split_top_level_statements(source: str) -> str:
    """Put same-line statements on separate lines before declaration inference."""
    mask = code_mask(source)
    output: list[str] = []
    offset = 0
    logical_line = 1

    for line in source.splitlines(keepends=True):
        directive = re.match(r'^[ \t]*#line[ \t]+(\d+)[ \t]+"main\.ecpp"', line)
        if directive:
            logical_line = int(directive.group(1))
            output.append(line)
            offset += len(line)
            continue
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            body, newline = line[:-1], line[-1]
        else:
            body, newline = line, ""

        round_depth = square_depth = brace_depth = 0
        separators: list[int] = []
        for index, character in enumerate(body):
            if not mask[offset + index]:
                continue
            if character == "(":
                round_depth += 1
            elif character == ")" and round_depth:
                round_depth -= 1
            elif character == "[":
                square_depth += 1
            elif character == "]" and square_depth:
                square_depth -= 1
            elif character == "{":
                brace_depth += 1
            elif character == "}" and brace_depth:
                brace_depth -= 1
            elif (
                character == ";"
                and round_depth == square_depth == brace_depth == 0
            ):
                separators.append(index)

        if not separators or (
            len(separators) == 1 and separators[-1] == len(body) - 1
        ):
            output.append(f'#line {logical_line} "main.ecpp"\n')
            output.append(line)
            offset += len(line)
            logical_line += 1
            continue

        indent = re.match(r"[ \t]*", body).group()
        start = 0
        for separator in separators:
            statement = body[start : separator + 1].strip()
            if statement:
                output.append(f'#line {logical_line} "main.ecpp"\n')
                output.append(indent + statement + (newline or "\n"))
            start = separator + 1
        remainder = body[start:].strip()
        if remainder:
            output.append(f'#line {logical_line} "main.ecpp"\n')
            output.append(indent + remainder + newline)
        offset += len(line)
        logical_line += 1

    return "".join(output)


def translate_inferred_for_loops(source: str) -> str:
    """Infer range-for bindings and evaluate each range exactly once."""
    binding = re.compile(
        r"\bfor[ \t]*\([ \t]*(?P<bracket>\["
        r"[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*\])"
        r"[ \t]*:"
    )
    variable = re.compile(
        r"\bfor[ \t]*\([ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*:"
    )
    mask = code_mask(source)
    matches: list[tuple[re.Match[str], str]] = []
    matches.extend(
        (match, "bracket")
        for match in binding.finditer(source)
        if mask[match.start()]
    )
    matches.extend(
        (match, "name")
        for match in variable.finditer(source)
        if mask[match.start()]
    )
    matches.sort(key=lambda item: item[0].start())

    replacements: list[tuple[int, int, str]] = []
    counter = 1
    for match, group in matches:
        opening = source.find("(", match.start(), match.end())
        closing = find_closing_paren(source, opening, mask)
        colon = match.end() - 1
        range_expression = source[colon + 1 : closing].strip()
        if not range_expression:
            continue
        while f"__g{counter}" in source:
            counter += 1
        range_name = f"__g{counter}"
        counter += 1
        declaration = source[match.start(group) : match.end(group)]
        replacement = (
            f"for(auto&& {range_name}=({range_expression});"
            f"auto {declaration}:{range_name})"
        )
        replacements.append((match.start(), closing + 1, replacement))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def find_closing_angle(source: str, opening: int, mask: list[bool]) -> int:
    """Find the `>` paired with a template argument list's opening `<`."""
    depth = 0
    for index in range(opening, len(source)):
        if not mask[index]:
            continue
        if source[index] == "<":
            depth += 1
        elif source[index] == ">":
            depth -= 1
            if depth == 0:
                return index
    raise TranslationError(f"unclosed template argument list at character {opening}")


def translate_vector_comprehensions(source: str) -> str:
    """Translate typed or deduced `vector name(iterable, mapper)` syntax."""
    declaration = re.compile(
        r"(?m)^(?P<prefix>[ \t]*(?:(?:const|constexpr|static|thread_local)"
        r"[ \t]+)*)(?P<vector>(?:std::)?vector)\b"
        r"(?P<template>[ \t]*<)?"
    )
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

    for match in declaration.finditer(source):
        if not mask[match.start()]:
            continue
        element_type: str | None = None
        if match.group("template"):
            angle_open = match.end() - 1
            angle_close = find_closing_angle(source, angle_open, mask)
            element_type = source[angle_open + 1 : angle_close].strip()
            if not element_type:
                continue
            position = angle_close + 1
        else:
            position = match.end()
        while position < len(source) and source[position] in " \t":
            position += 1
        name_match = re.match(r"[A-Za-z_]\w*", source[position:])
        if not name_match:
            continue
        position += name_match.end()
        while position < len(source) and source[position] in " \t":
            position += 1
        if position >= len(source) or source[position] != "(":
            continue

        arguments_end = find_closing_paren(source, position, mask)
        arguments = split_top_level_commas(source[position + 1 : arguments_end])
        if len(arguments) != 2 or not all(argument.strip() for argument in arguments):
            continue
        iterable, mapper = (argument.strip() for argument in arguments)
        template_arguments = f"<{element_type}>" if element_type else ""
        replacement = (
            f"__V{template_arguments}("
            f"{iterable}, {mapper})"
        )
        replacements.append((position + 1, arguments_end, replacement))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_iota_calls(source: str) -> str:
    """Route unqualified ecpp `iota(...)` calls away from `std::iota`."""
    call = re.compile(r"(?<![\w.:>])iota(?P<spacing>[ \t]*)\(")
    mask = code_mask(source)
    matches = [match for match in call.finditer(source) if mask[match.start()]]
    for match in reversed(matches):
        replacement = f"__I{match.group('spacing')}("
        source = source[: match.start()] + replacement + source[match.end() :]
    return source


def translate_rep_shorthand(source: str) -> str:
    """Give every one-argument `rep(count)` a unique hidden loop variable."""
    call = re.compile(r"(?<![\w.:>])rep[ \t]*\(")
    mask = code_mask(source)
    insertions: list[tuple[int, str]] = []
    counter = 1

    for match in call.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        argument_text = source[opening + 1 : closing]
        argument_mask = code_mask(argument_text)
        masked_arguments = "".join(
            character if argument_mask[index] else " "
            for index, character in enumerate(argument_text)
        )
        arguments = split_top_level_commas(masked_arguments)
        if len(arguments) != 1 or not argument_text.strip():
            continue
        insertions.append((opening + 1, f"__p{counter}, "))
        counter += 1

    for position, insertion in reversed(insertions):
        source = source[:position] + insertion + source[position:]
    return source


def translate_loop_macros(source: str) -> str:
    """Expand loop shorthands with every bound evaluated exactly once."""
    call = re.compile(
        r"(?<![\w.:>])(?P<name>rep|range|rrep|srange|rrange|rsrange|subset)"
        r"[ \t]*\("
    )
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []
    counter = 1
    arities = {"rep": 2, "range": 3, "rrep": 2, "srange": 4,
               "rrange": 3, "rsrange": 4, "subset": 2}
    for match in call.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        arguments = [part.strip() for part in split_top_level_commas(
            source[opening + 1 : closing]
        )]
        name = match.group("name")
        if len(arguments) != arities[name] or not all(arguments):
            continue
        variable, *values = arguments
        if not re.fullmatch(r"[A-Za-z_]\w*", variable):
            continue
        while f"__l{counter}_" in source:
            counter += 1
        prefix = f"__l{counter}_"
        counter += 1
        if name == "rep":
            replacement = (
                f"for(int {variable}=0,{prefix}end=({values[0]});"
                f"{variable}<{prefix}end;++{variable})"
            )
        elif name == "range":
            replacement = (
                f"for(int {variable}=({values[0]}),{prefix}end=({values[1]});"
                f"{variable}<{prefix}end;++{variable})"
            )
        elif name == "rrep":
            replacement = f"for(int {variable}=({values[0]})-1;{variable}>=0;--{variable})"
        elif name == "rrange":
            replacement = (
                f"for(int {prefix}begin=({values[0]}),{variable}=({values[1]})-1;"
                f"{variable}>={prefix}begin;--{variable})"
            )
        elif name == "srange":
            replacement = (
                f"for(int {variable}=({values[0]}),{prefix}end=({values[1]}),"
                f"{prefix}step=({values[2]});{variable}<{prefix}end;"
                f"{variable}+={prefix}step)"
            )
        elif name == "rsrange":
            replacement = (
                f"for(int {prefix}begin=({values[0]}),{variable}=({values[1]})-1,"
                f"{prefix}step=({values[2]});{variable}>={prefix}begin;"
                f"{variable}-={prefix}step)"
            )
        else:
            replacement = (
                f"for(int {prefix}mask=({values[0]}),{variable}={prefix}mask;"
                f"{variable}>=0;{variable}={variable}?"
                f"({variable}-1)&{prefix}mask:-1)"
            )
        replacements.append((match.start(), closing + 1, replacement))
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_nonloop_macros(source: str) -> str:
    """Expand expression and statement shorthands formerly implemented as macros."""
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []
    tokens = {
        "elif": "else if",
        "kill": '{debug("killed");assert(0);}',
        "Seg": "segtree_<S,[](S a,S b){return op(a,b);}>",
        "LazySeg": (
            "lazysegtree_<S,[](S a,S b){return op(a,b);},F,"
            "[](F f,S x){return mapping(f,x);},"
            "[](F f,F g){return composition(f,g);}>"
        ),
    }
    token_pattern = re.compile(r"\b(?:elif|kill|Seg|LazySeg)\b")
    for match in token_pattern.finditer(source):
        if mask[match.start()]:
            replacements.append((match.start(), match.end(), tokens[match.group()]))
    call_pattern = re.compile(r"(?<![\w.:>])(?P<name>min|max)[ \t]*\(")
    for match in call_pattern.finditer(source):
        if mask[match.start()]:
            replacements.append(
                (match.start(), match.end() - 1, f"my_{match.group('name')}")
            )
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]

    mask = code_mask(source)
    replacements = []
    call_pattern = re.compile(r"(?<![\w.:>])(?P<name>finish|stf)[ \t]*\(")
    counter = 1
    for match in call_pattern.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        name = match.group("name")
        argument = source[opening + 1 : closing].strip()
        if not argument:
            continue
        if name == "finish":
            replacement = f"{{print(({argument}));return;}}"
        else:
            hidden = f"__T{counter}"
            counter += 1
            replacement = (
                f"[&,{hidden}=({argument})](auto&& y)->decltype(auto)"
                f"{{return {hidden}(std::forward<decltype(y)>(y));}}"
            )
        replacements.append((match.start(), closing + 1, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def translate_all_calls(source: str) -> str:
    """Expand `all(expr)` inside a call while evaluating expr exactly once."""
    counter = 1
    pattern = re.compile(r"(?<![\w.:>])all[ \t]*\(")
    while True:
        mask = code_mask(source)
        matches = [match for match in pattern.finditer(source) if mask[match.start()]]
        if not matches:
            return source
        match = matches[-1]
        all_open = match.end() - 1
        all_close = find_closing_paren(source, all_open, mask)
        stack: list[int] = []
        candidates: list[int] = []
        for index in range(match.start()):
            if not mask[index]:
                continue
            if source[index] == "(":
                stack.append(index)
            elif source[index] == ")" and stack:
                stack.pop()
        for opening in reversed(stack):
            before = opening - 1
            while before >= 0 and source[before] in " \t":
                before -= 1
            if before >= 0 and (
                source[before].isalnum() or source[before] in "_)]>"
            ):
                candidates.append(opening)
                break
        if not candidates:
            raise TranslationError("all(expr) must be used inside a function call")
        call_open = candidates[0]
        call_close = find_closing_paren(source, call_open, mask)
        if all_close > call_close:
            raise TranslationError("could not determine the call containing all(expr)")
        call_start = receiver_start(source, call_open, mask)
        expression = source[all_open + 1 : all_close].strip()
        if not expression:
            raise TranslationError("all() requires a range expression")
        while f"__a{counter}" in source:
            counter += 1
        hidden = f"__a{counter}"
        counter += 1
        call = source[call_start : call_close + 1]
        relative_start = match.start() - call_start
        relative_end = all_close + 1 - call_start
        expanded = (
            call[:relative_start]
            + f"{hidden}.begin(),{hidden}.end()"
            + call[relative_end:]
        )
        replacement = (
            f"([&](auto&& {hidden})->decltype(auto){{return {expanded};}})"
            f"(({expression}))"
        )
        source = source[:call_start] + replacement + source[call_close + 1 :]


def translate_macro_syntax(source: str) -> str:
    source = translate_rep_shorthand(source)
    source = translate_loop_macros(source)
    source = translate_nonloop_macros(source)
    return translate_all_calls(source)


def translate_sort_calls(source: str) -> str:
    """Allow one range argument or a range followed by a comparator for sort."""
    call = re.compile(r"(?<![\w.:>])sort[ \t]*\(")
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

    for match in call.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        argument_text = source[opening + 1 : closing]
        argument_mask = code_mask(argument_text)
        masked_arguments = "".join(
            character if argument_mask[index] else " "
            for index, character in enumerate(argument_text)
        )
        arguments = split_top_level_commas(masked_arguments)
        if len(arguments) not in (1, 2) or not all(
            argument.strip() for argument in arguments
        ):
            continue
        replacements.append((match.start(), opening, "std::ranges::sort"))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_reverse_calls(source: str) -> str:
    """Allow a single range argument for `reverse`."""
    call = re.compile(r"(?<![\w.:>])reverse[ \t]*\(")
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

    for match in call.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        argument_text = source[opening + 1 : closing]
        argument_mask = code_mask(argument_text)
        masked_arguments = "".join(
            character if argument_mask[index] else " "
            for index, character in enumerate(argument_text)
        )
        arguments = split_top_level_commas(masked_arguments)
        if len(arguments) != 1 or not arguments[0].strip():
            continue
        replacements.append((match.start(), opening, "std::ranges::reverse"))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_bound_calls(source: str) -> str:
    """Allow `lower_bound(range, value)` and `upper_bound(range, value)`."""
    call = re.compile(r"(?<![\w.:>])(?P<name>lower_bound|upper_bound)[ \t]*\(")
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

    for match in call.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        argument_text = source[opening + 1 : closing]
        argument_mask = code_mask(argument_text)
        masked_arguments = "".join(
            character if argument_mask[index] else " "
            for index, character in enumerate(argument_text)
        )
        arguments = split_top_level_commas(masked_arguments)
        if len(arguments) != 2 or not all(
            argument.strip() for argument in arguments
        ):
            continue
        replacements.append(
            (match.start(), opening, f"std::ranges::{match.group('name')}")
        )

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


BUILTIN_TYPE_NAMES = {
    "auto",
    "bool",
    "char",
    "double",
    "float",
    "function",
    "int",
    "ld",
    "ll",
    "long",
    "mint",
    "map",
    "pair",
    "priority_queue",
    "short",
    "signed",
    "size_t",
    "str",
    "set",
    "stmap",
    "stset",
    "tuple",
    "uomap",
    "uoset",
    "unsigned",
    "vec",
    "vector",
    "void",
}


def translate_container_type_names(source: str) -> str:
    """Use hash containers by default and `st*` names for ordered ones."""
    aliases = {
        "map": "uomap",
        "set": "uoset",
        "stmap": "std::map",
        "stset": "std::set",
    }
    pattern = re.compile(r"\b(stmap|stset|map|set)(?=[ \t\r\n]*<)")
    mask = code_mask(source)
    output: list[str] = []
    position = 0
    for match in pattern.finditer(source):
        if not mask[match.start()]:
            continue
        output.append(source[position : match.start()])
        output.append(aliases[match.group(1)])
        position = match.end()
    output.append(source[position:])
    return "".join(output)


def declared_type_names(source: str) -> set[str]:
    """Collect user type names that may begin an explicit declaration."""
    names = set(BUILTIN_TYPE_NAMES)
    mask = code_mask(source)
    patterns = (
        r"\busing[ \t]+([A-Za-z_]\w*)[ \t]*=",
        r"\b(?:class|struct|union)[ \t]+([A-Za-z_]\w*)",
        r"\benum(?:[ \t]+class)?[ \t]+([A-Za-z_]\w*)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source):
            if mask[match.start()]:
                names.add(match.group(1))
    return names


def find_lambda_expressions(
    source: str,
) -> list[tuple[int, int, int | None, int | None, int, int]]:
    """Locate lambda captures, optional parameter lists, and bodies."""
    mask = code_mask(source)
    result = []
    suffix_pattern = re.compile(
        r"\s*(?:(?:mutable|constexpr|consteval|static)\s+)*"
        r"(?:noexcept(?:\s*\([^()\n]*\))?\s*)?"
        r"(?:->[^{;\n]+\s*)?(?:requires[^{;\n]+\s*)?\{"
    )

    for capture_open, character in enumerate(source):
        if character != "[" or not mask[capture_open]:
            continue
        if capture_open > 0 and (
            source[capture_open - 1].isalnum()
            or source[capture_open - 1] in "_])"
        ):
            continue
        capture_close = find_closing_bracket(source, capture_open, mask)
        position = capture_close + 1
        while position < len(source) and source[position].isspace():
            position += 1

        # Preserve explicit lambda template parameter lists, when present.
        if position < len(source) and source[position] == "<":
            depth = 0
            for index in range(position, len(source)):
                if not mask[index]:
                    continue
                if source[index] == "<":
                    depth += 1
                elif source[index] == ">":
                    depth -= 1
                    if depth == 0:
                        position = index + 1
                        break
            else:
                continue
            while position < len(source) and source[position].isspace():
                position += 1

        parameters_open: int | None = None
        parameters_close: int | None = None
        if position < len(source) and source[position] == "(":
            parameters_open = position
            parameters_close = find_closing_paren(source, position, mask)
            suffix_start = parameters_close + 1
        else:
            suffix_start = position

        lambda_suffix = suffix_pattern.match(source[suffix_start:])
        if not lambda_suffix:
            continue
        body_open = suffix_start + lambda_suffix.end() - 1
        if body_open >= len(mask) or not mask[body_open]:
            continue
        body_close = find_closing_brace(source, body_open, mask)
        result.append(
            (
                capture_open,
                capture_close,
                parameters_open,
                parameters_close,
                body_open,
                body_close,
            )
        )
    return result


def translate_lambda_parameter_types(source: str, type_names: set[str]) -> str:
    """Use reference capture for `[]` and add `auto` to bare parameters."""
    replacements: list[tuple[int, int, str]] = []

    for (
        capture_open,
        capture_close,
        parameters_open,
        parameters_close,
        _,
        _,
    ) in find_lambda_expressions(source):
        if not source[capture_open + 1 : capture_close].strip():
            replacements.append((capture_open, capture_close + 1, "[&]"))

        if parameters_open is None or parameters_close is None:
            continue
        parameter_text = source[parameters_open + 1 : parameters_close]
        if not parameter_text.strip():
            continue
        parameters = split_top_level_commas(parameter_text)
        translated: list[str] = []
        changed = False
        for parameter in parameters:
            bare = re.fullmatch(
                r"\s*(?P<prefix>&&|&|\*)?[ \t]*"
                r"(?P<name>[A-Za-z_]\w*)"
                r"(?P<default>[ \t]*=.*)?\s*",
                parameter,
            )
            if not bare:
                translated.append(parameter.strip())
                continue
            name = bare.group("name")
            if name in type_names or name[:1].isupper():
                translated.append(parameter.strip())
                continue
            prefix = bare.group("prefix") or ""
            default = bare.group("default") or ""
            spacing = "" if prefix else " "
            translated.append(f"auto{prefix}{spacing}{name}{default}")
            changed = True

        if changed:
            replacements.append(
                (parameters_open + 1, parameters_close, ", ".join(translated))
            )

    for start, end, replacement in sorted(replacements, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def translate_function_parameter_types(source: str, type_names: set[str]) -> str:
    """Add `auto` to bare parameters of ordinary function definitions."""
    header = re.compile(
        r"(?m)^[ \t]*(?:template[ \t]*<[^{}\r\n]+>[ \t]*)?"
        r"[A-Za-z_~][\w:<>,*& \t]*?[ \t]+"
        r"(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*[ \t]*\("
    )
    suffix = re.compile(
        r"^[ \t]*(?:(?:const|noexcept|override|final)[ \t]*)*"
        r"(?:->[^{}\r\n]+)?[ \t]*\{"
    )
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []
    for match in header.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        if not suffix.match(source[closing + 1 :]):
            continue
        parameter_text = source[opening + 1 : closing]
        if not parameter_text.strip():
            continue
        parameters = split_top_level_commas(parameter_text)
        translated: list[str] = []
        changed = False
        for parameter in parameters:
            bare = re.fullmatch(
                r"\s*(?P<prefix>&&|&|\*)?[ \t]*"
                r"(?P<name>[A-Za-z_]\w*)"
                r"(?P<default>[ \t]*=.*)?\s*",
                parameter,
            )
            if not bare:
                translated.append(parameter.strip())
                continue
            name = bare.group("name")
            if name in type_names or name[:1].isupper():
                translated.append(parameter.strip())
                continue
            prefix = bare.group("prefix") or ""
            default = bare.group("default") or ""
            spacing = "" if prefix else " "
            translated.append(f"auto{prefix}{spacing}{name}{default}")
            changed = True
        if changed:
            replacements.append((opening + 1, closing, ", ".join(translated)))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def translate_function_return_types(source: str) -> str:
    """Give return-type-less ordinary function definitions an `auto` type."""
    header = re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)[ \t]*\("
    )
    suffix = re.compile(
        r"^[ \t]*(?:(?:const|noexcept|override|final)[ \t]*)*"
        r"(?:->[^{}\r\n]+)?[ \t]*\{"
    )
    excluded = {
        "if", "for", "while", "switch", "catch", "rep", "rrep",
        "range", "rrange", "srange", "rsrange", "subset",
    }
    mask = code_mask(source)
    insertions: list[int] = []
    for match in header.finditer(source):
        if not mask[match.start()] or match.group("name") in excluded:
            continue
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        if suffix.match(source[closing + 1 :]):
            insertions.append(match.start("name"))
    for position in reversed(insertions):
        source = source[:position] + "auto " + source[position:]
    return source


def implicit_lambda_return_body(body: str, type_names: set[str]) -> str:
    """Return the final top-level expression, leaving final operations alone."""
    mask = code_mask(body)
    comments = comment_mask(body)
    round_depth = square_depth = brace_depth = 0
    semicolons: list[int] = []
    brace_closings: list[int] = []
    for index, character in enumerate(body):
        if not mask[index]:
            continue
        if character == "(":
            round_depth += 1
        elif character == ")" and round_depth:
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]" and square_depth:
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
            if round_depth == square_depth == brace_depth == 0:
                brace_closings.append(index)
        elif (
            character == ";"
            and round_depth == square_depth == brace_depth == 0
        ):
            semicolons.append(index)

    code_positions = [
        index
        for index, character in enumerate(body)
        if not comments[index] and not character.isspace()
    ]
    if not code_positions:
        return body

    last_code = code_positions[-1]
    has_trailing_semicolon = body[last_code] == ";" and last_code in semicolons
    statement_end = last_code if has_trailing_semicolon else last_code + 1
    earlier_semicolons = [
        position for position in semicolons if position < statement_end
    ]
    statement_start = (earlier_semicolons[-1] + 1) if earlier_semicolons else 0
    for closing in brace_closings:
        following = next(
            (
                position
                for position in code_positions
                if closing < position < statement_end
            ),
            None,
        )
        if following is None:
            continue
        # A top-level block followed by a fresh token ends the preceding
        # control statement. Do not split postfix use of a lambda or object.
        if body[following].isalnum() or body[following] in "_\"'[$":
            statement_start = max(statement_start, closing + 1)
    statement_code = [
        position
        for position in code_positions
        if statement_start <= position < statement_end
    ]
    if not statement_code:
        return body
    first_code, expression_last = statement_code[0], statement_code[-1]
    clean = "".join(
        body[index] if not comments[index] else " "
        for index in range(first_code, expression_last + 1)
    ).strip()
    if not clean:
        return body

    def terminate_statement() -> str:
        if has_trailing_semicolon:
            return body
        return body[: expression_last + 1] + ";" + body[expression_last + 1 :]

    statement_keyword = re.match(r"([A-Za-z_]\w*)", clean)
    if statement_keyword and statement_keyword.group(1) in {
        "break",
        "case",
        "catch",
        "co_return",
        "co_yield",
        "continue",
        "default",
        "do",
        "else",
        "for",
        "goto",
        "if",
        "range",
        "rep",
        "return",
        "rrep",
        "srange",
        "switch",
        "throw",
        "try",
        "typedef",
        "using",
        "while",
    }:
        return terminate_statement()

    # A type followed by a declarator is a declaration, while constructions
    # such as `pair<int,int>{x,y}` remain expressions and are returned.
    type_pattern = "|".join(
        sorted((re.escape(name) for name in type_names), key=len, reverse=True)
    )
    inferred_type = rf"(?:{type_pattern}|[A-Z][A-Za-z_0-9]*)"
    declaration = re.compile(
        r"^(?:(?:const|volatile|static|constexpr|consteval|inline)\s+)*"
        rf"{inferred_type}\b(?:\s*<[^;{{}}]*>)?"
        r"(?:\s+(?:const\s+)?|\s*[*&]+\s*)"
        r"[A-Za-z_]\w*(?:\s|=|\[|\(|\{|$)"
    )
    if declaration.match(clean):
        return terminate_statement()

    # Keep a final mutation as a statement. Operators nested in calls,
    # subscripts, braced initializers, or nested lambdas do not count here.
    expression = body[first_code : expression_last + 1]
    expression_mask = code_mask(expression)
    round_depth = square_depth = brace_depth = 0
    for index, character in enumerate(expression):
        if not expression_mask[index]:
            continue
        if character == "(":
            round_depth += 1
        elif character == ")" and round_depth:
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]" and square_depth:
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
        elif round_depth == square_depth == brace_depth == 0:
            pair = expression[index : index + 2]
            if pair in {"++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^="}:
                return terminate_statement()
            if character == "=":
                previous = expression[index - 1] if index else ""
                following = expression[index + 1] if index + 1 < len(expression) else ""
                if previous not in "<>=!" and following not in "=>":
                    return terminate_statement()

    result = body[:first_code] + "return " + body[first_code:]
    if not has_trailing_semicolon:
        insertion = expression_last + len("return ") + 1
        result = result[:insertion] + ";" + result[insertion:]
    return result


def translate_lambda_implicit_returns(source: str, type_names: set[str]) -> str:
    """Add an implicit return to the last expression of every lambda."""
    while True:
        changed = False
        candidates = sorted(
            find_lambda_expressions(source), key=lambda item: item[4], reverse=True
        )
        for _, _, _, _, body_open, body_close in candidates:
            body = source[body_open + 1 : body_close]
            translated = implicit_lambda_return_body(body, type_names)
            if translated == body:
                continue
            source = source[: body_open + 1] + translated + source[body_close:]
            changed = True
            break
        if not changed:
            return source


def translate_function_implicit_returns(source: str, type_names: set[str]) -> str:
    """Add an implicit return to the final expression of ordinary functions."""
    function = re.compile(
        r"(?m)^(?P<header>[ \t]*(?:template[ \t]*<[^{}\r\n]+>[ \t]*)?"
        r"(?P<prefix>[^#;{}\r\n]*?)[ \t]+(?P<name>[A-Za-z_]\w*)"
        r"[ \t]*\([^;{}\r\n]*\)[ \t]*"
        r"(?:(?:const|noexcept|override|final)[ \t]*)*"
        r"(?:->[^{}\r\n]+)?)[ \t]*\{"
    )
    excluded = {"if", "for", "while", "switch", "catch", "rep", "range"}
    while True:
        mask = code_mask(source)
        candidates: list[tuple[int, int]] = []
        for match in function.finditer(source):
            if not mask[match.start()] or match.group("name") in excluded:
                continue
            if "[" in match.group("header"):
                continue
            if re.search(r"\bvoid[ \t]*$", match.group("prefix")):
                continue
            opening = match.end() - 1
            candidates.append((opening, find_closing_brace(source, opening, mask)))
        changed = False
        for opening, closing in reversed(candidates):
            body = source[opening + 1 : closing]
            translated = implicit_lambda_return_body(body, type_names)
            if translated == body:
                continue
            source = source[: opening + 1] + translated + source[closing:]
            changed = True
            break
        if not changed:
            return source


def split_top_level_commas(text: str) -> list[str]:
    """Split declarators while ignoring commas inside paired delimiters."""
    parts: list[str] = []
    start = 0
    round_depth = square_depth = brace_depth = angle_depth = 0
    for index, character in enumerate(text):
        if character == "(":
            round_depth += 1
        elif character == ")" and round_depth:
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]" and square_depth:
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}" and brace_depth:
            brace_depth -= 1
        elif character == "<":
            angle_depth += 1
        elif character == ">" and angle_depth:
            angle_depth -= 1
        elif (
            character == ","
            and round_depth == square_depth == brace_depth == angle_depth == 0
        ):
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def translate_record_aliases(source: str) -> str:
    """Translate `using S = {T field, U other};` into an aggregate struct."""
    declaration = re.compile(
        r"\busing[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*\{"
    )
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

    for match in declaration.finditer(source):
        if not mask[match.start()]:
            continue
        opening = match.end() - 1
        depth = 0
        closing: int | None = None
        for index in range(opening, len(source)):
            if not mask[index]:
                continue
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing is None:
            raise TranslationError(
                f"unclosed record declaration at character {match.start()}"
            )

        fields_text = source[opening + 1 : closing]
        fields = [field.strip() for field in split_top_level_commas(fields_text)]
        if len(fields) == 1 and not fields[0]:
            fields = []
        if any(not field for field in fields):
            raise TranslationError(
                f"empty field in record declaration {match.group('name')}"
            )

        end = closing + 1
        while end < len(source) and source[end] in " \t":
            end += 1
        if end < len(source) and source[end] == ";":
            end += 1
        field_declarations = " ".join(f"{field};" for field in fields)
        spacing = " " if field_declarations else ""
        replacement = (
            f"struct {match.group('name')} {{{spacing}{field_declarations} }};"
        )
        replacements.append((match.start(), end, replacement))

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source


def binding_names(code: str) -> list[str]:
    """Return names introduced by a leading `let` or `auto`."""
    match = re.match(r"^[ \t]*(?:let|auto)[ \t]*\[([^\]\n]+)\]", code)
    if match:
        names = [name.strip() for name in match.group(1).split(",")]
        if all(re.fullmatch(r"[A-Za-z_]\w*", name) for name in names):
            return names
        return []

    match = re.match(
        r"^[ \t]*(?:let|auto)[ \t]+"
        r"([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*)"
        r"[ \t]*(?==|:|\{|\()",
        code,
    )
    if not match:
        return []
    return re.findall(r"[A-Za-z_]\w*", match.group(1))


def explicit_declaration_names(code: str, type_names: set[str]) -> list[str]:
    """Recognize common C++ declarations at the beginning of a line."""
    stripped = code.lstrip()
    qualifier = re.compile(
        r"(?:const|constexpr|consteval|constinit|static|volatile|extern|"
        r"register|mutable|thread_local)[ \t]+"
    )
    while True:
        match = qualifier.match(stripped)
        if not match:
            break
        stripped = stripped[match.end() :]

    type_match = re.match(
        r"(?P<type>(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)", stripped
    )
    if not type_match:
        return []
    type_name = type_match.group("type").split("::")[-1]
    position = type_match.end()
    has_template = position < len(stripped) and stripped[position] == "<"
    # A template declaration remains recognizable without whitespace between
    # its closing `>` and the variable name, even for a lowercase custom type.
    if (
        type_name not in type_names
        and not type_name[:1].isupper()
        and not has_template
    ):
        return []

    if has_template:
        depth = 0
        while position < len(stripped):
            character = stripped[position]
            if character == "<":
                depth += 1
            elif character == ">":
                depth -= 1
                if depth == 0:
                    position += 1
                    break
            position += 1
        if depth:
            return []
    elif position < len(stripped) and not stripped[position].isspace():
        return []

    while position < len(stripped) and stripped[position].isspace():
        position += 1
    while position < len(stripped) and stripped[position] in "*&":
        position += 1
        while position < len(stripped) and stripped[position].isspace():
            position += 1

    declarators = stripped[position:]
    names: list[str] = []
    for declarator in split_top_level_commas(declarators):
        match = re.match(r"[ \t]*(?:[*&][ \t]*)*([A-Za-z_]\w*)", declarator)
        if match:
            names.append(match.group(1))
    return names


def translate_python_list_literals(
    source: str, defined_names: set[str], type_names: set[str]
) -> str:
    """Translate unambiguous Python-style list values to CTAD `vec{...}`."""
    non_variables = type_names | {
        "alignof",
        "and",
        "bitand",
        "bitor",
        "bool",
        "char",
        "const",
        "decltype",
        "false",
        "float",
        "int",
        "ld",
        "ll",
        "long",
        "new",
        "noexcept",
        "not",
        "nullptr",
        "or",
        "short",
        "signed",
        "sizeof",
        "static_cast",
        "str",
        "true",
        "unsigned",
    }

    while True:
        mask = code_mask(source)
        stack: list[int] = []
        pairs: list[tuple[int, int]] = []
        for index, character in enumerate(source):
            if not mask[index]:
                continue
            if character == "[":
                stack.append(index)
            elif character == "]" and stack:
                pairs.append((stack.pop(), index))

        replacement: tuple[int, int, str] | None = None
        for opening, closing in sorted(pairs, reverse=True):
            previous = opening - 1
            while previous >= 0 and source[previous].isspace():
                previous -= 1
            if previous >= 0 and (
                source[previous].isalnum()
                or source[previous] in "_)]}'\""
            ):
                token = re.search(r"([A-Za-z_]\w*)[ \t]*$", source[:opening])
                if not token or token.group(1) not in {"return", "co_return"}:
                    continue

            following = closing + 1
            while following < len(source) and source[following].isspace():
                following += 1
            if following < len(source):
                if source[following] == ":":
                    continue
                if source[following] == "=" and not source.startswith(
                    "==", following
                ):
                    continue
                if source[following] in "({":
                    continue
                if re.match(
                    r"(?:mutable|constexpr|consteval|static|noexcept)\b|->",
                    source[following:],
                ):
                    continue

            content = source[opening + 1 : closing]
            content_mask = code_mask(content)
            valid = True
            for identifier in re.finditer(r"\b[A-Za-z_]\w*\b", content):
                if not content_mask[identifier.start()]:
                    continue
                name = identifier.group(0)
                if name in defined_names or name in non_variables:
                    continue

                before = identifier.start() - 1
                while before >= 0 and content[before].isspace():
                    before -= 1
                after = identifier.end()
                while after < len(content) and content[after].isspace():
                    after += 1
                is_member = before >= 0 and content[before] == "."
                is_arrow_member = (
                    before >= 1 and content[before - 1 : before + 1] == "->"
                )
                is_qualified = (
                    before >= 1 and content[before - 1 : before + 1] == "::"
                ) or content.startswith("::", after)
                is_callable_or_type = (
                    after < len(content) and content[after] in "({"
                )
                if (
                    is_member
                    or is_arrow_member
                    or is_qualified
                    or is_callable_or_type
                ):
                    continue
                valid = False
                break

            if valid:
                replacement = (opening, closing + 1, f"vec{{{content}}}")
                break

        if replacement is None:
            return source
        start, end, value = replacement
        source = source[:start] + value + source[end:]


def translate_implicit_let_declarations(
    source: str, type_names: set[str]
) -> str:
    """Infer declarations for leading value and reference assignments."""
    mask = code_mask(source)
    scopes: list[set[str]] = [set()]
    output: list[str] = []
    offset = 0

    def is_defined(name: str) -> bool:
        return any(name in scope for scope in reversed(scopes))

    for line in source.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]

        code = "".join(
            character if mask[offset + index] else " "
            for index, character in enumerate(body)
        )
        leading_closures = re.match(r"^[ \t]*(?:}[ \t]*)+", code)
        brace_scan_start = 0
        if leading_closures:
            for _ in range(leading_closures.group(0).count("}")):
                if len(scopes) > 1:
                    scopes.pop()
            brace_scan_start = leading_closures.end()
        defined_before = set().union(*scopes)

        list_match = re.match(
            r"^(?P<indent>[ \t]*)\["
            r"(?P<names>[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)+)"
            r"\][ \t]*=(?!=)",
            code,
        )
        reference_match = re.match(
            r"^(?P<indent>[ \t]*)&[ \t]*(?P<name>[A-Za-z_]\w*)"
            r"[ \t]*=(?!=)",
            code,
        )
        name_match = re.match(
            r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)[ \t]*=(?!=)",
            code,
        )

        inserted_names: list[str] = []
        indent_length = 0
        if list_match:
            candidates = re.findall(r"[A-Za-z_]\w*", list_match.group("names"))
            if len(set(candidates)) == len(candidates) and all(
                not is_defined(candidate) for candidate in candidates
            ):
                inserted_names = candidates
                indent_length = len(list_match.group("indent"))
        elif reference_match and not is_defined(reference_match.group("name")):
            inserted_names = [reference_match.group("name")]
            indent_length = len(reference_match.group("indent"))
        elif name_match and not is_defined(name_match.group("name")):
            inserted_names = [name_match.group("name")]
            indent_length = len(name_match.group("indent"))

        body = translate_python_list_literals(body, defined_before, type_names)
        body_mask = code_mask(body)
        code = "".join(
            character if body_mask[index] else " "
            for index, character in enumerate(body)
        )

        if inserted_names:
            body = body[:indent_length] + "let " + body[indent_length:]
            code = code[:indent_length] + "let " + code[indent_length:]
            scopes[-1].update(inserted_names)

        scopes[-1].update(binding_names(code))
        scopes[-1].update(explicit_declaration_names(code, type_names))
        block_names: set[str] = set()
        for loop_match in re.finditer(
            r"\b(?:rep|rrep)[ \t]*\([ \t]*([A-Za-z_]\w*)[ \t]*,",
            code,
        ):
            block_names.add(loop_match.group(1))
        for loop_match in re.finditer(
            r"\bfor[ \t]*\([^;:\n]*\b([A-Za-z_]\w*)[ \t]*(?:=|:)",
            code,
        ):
            block_names.add(loop_match.group(1))
        for loop_match in re.finditer(
            r"\bfor[ \t]*\([ \t]*(?:let|auto)[ \t]*"
            r"\[([^\]\n]+)\][ \t]*:",
            code,
        ):
            block_names.update(re.findall(r"[A-Za-z_]\w*", loop_match.group(1)))

        opening = code.find("(")
        if opening >= 0 and "{" in code[opening:]:
            depth = 0
            closing = -1
            for index in range(opening, len(code)):
                if code[index] == "(":
                    depth += 1
                elif code[index] == ")":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing >= 0:
                for parameter in split_top_level_commas(code[opening + 1 : closing]):
                    block_names.update(
                        explicit_declaration_names(parameter.strip(), type_names)
                    )

        for character in code[brace_scan_start:]:
            if character == "{":
                scopes.append(set())
                if block_names:
                    scopes[-1].update(block_names)
                    block_names.clear()
            elif character == "}" and len(scopes) > 1:
                scopes.pop()

        output.append(body + newline)
        offset += len(line)

    return "".join(output)


def translate_let_declarations(source: str) -> str:
    """Translate `let` only when it has the shape of a variable declaration."""
    source = translate_bracket_let_declarations(source)
    source = translate_bracket_assignments(source)
    source = translate_multiple_let_declarations(source)
    # `input()` is context-convertible, but `auto x = input()` cannot provide
    # that context.  An untyped input declaration therefore defaults to str,
    # matching Python's input() and the rest of the ECPP input shorthand.
    untyped_input = re.compile(
        r"\blet(?P<spacing>[ \t]+)(?P<name>[A-Za-z_]\w*)"
        r"(?P<equals>[ \t]*=[ \t]*)input[ \t]*\([ \t]*\)"
        r"(?=[ \t]*(?:;|//|[\r\n]|$))"
    )
    input_mask = code_mask(source)
    input_matches = [
        match
        for match in untyped_input.finditer(source)
        if input_mask[match.start()]
    ]
    for match in reversed(input_matches):
        replacement = (
            f"let{match.group('spacing')}{match.group('name')}"
            f"{match.group('equals')}str(input())"
        )
        source = source[: match.start()] + replacement + source[match.end() :]
    declaration = re.compile(
        r"\blet(?=(?:[ \t]+[A-Za-z_]\w*|[ \t]*&[ \t]*[A-Za-z_]\w*|"
        r"[ \t]*\[[^\]\n]+\])"
        r"[ \t]*(?:=|:|\{|\())"
    )
    mask = code_mask(source)
    matches = []
    for match in declaration.finditer(source):
        if not mask[match.start()]:
            continue
        previous = match.start() - 1
        while previous >= 0 and source[previous] in " \t":
            previous -= 1
        if previous >= 0 and source[previous] in ".:>":
            continue
        matches.append(match)

    for match in reversed(matches):
        source = source[: match.start()] + "auto" + source[match.end() :]
    return source


def translate_bracket_let_declarations(source: str) -> str:
    """Allow bracket bindings to unpack both tuple-like and indexable values."""
    code_comments = comment_mask(source)
    declaration = re.compile(
        r"^(?P<indent>[ \t]*)let[ \t]*\["
        r"(?P<names>[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*)"
        r"\][ \t]*=[ \t]*(?P<value>.+?)$"
    )

    output: list[str] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]

        comment_at = len(body)
        for index in range(len(body) - 1):
            if body[index : index + 2] == "//" and code_comments[offset + index]:
                comment_at = index
                break

        statement = body[:comment_at]
        comment = body[comment_at:]
        trailing = statement[len(statement.rstrip()) :]
        statement = statement.rstrip()
        remainder = ""
        statement_mask = code_mask(statement)
        round_depth = square_depth = brace_depth = 0
        for index, character in enumerate(statement):
            if not statement_mask[index]:
                continue
            if character == "(":
                round_depth += 1
            elif character == ")" and round_depth:
                round_depth -= 1
            elif character == "[":
                square_depth += 1
            elif character == "]" and square_depth:
                square_depth -= 1
            elif character == "{":
                brace_depth += 1
            elif character == "}" and brace_depth:
                brace_depth -= 1
            elif (
                character == ";"
                and round_depth == square_depth == brace_depth == 0
            ):
                remainder = statement[index + 1 :]
                statement = statement[:index].rstrip()
                break

        match = declaration.match(statement)
        if match:
            names = [name.strip() for name in match.group("names").split(",")]
            requirements = " ".join(
                f"__u[{index}];" for index in range(len(names))
            )
            elements = ", ".join(
                f"__u[{index}]" for index in range(len(names))
            )
            statement = (
                f"{match.group('indent')}auto [{match.group('names')}] = "
                "([&](auto&& __u) { "
                f"if constexpr (requires {{ {requirements} }}) {{ "
                f"return std::tuple{{{elements}}}; "
                "} else { return std::forward<decltype(__u)>("
                "__u); } "
                f"}})({match.group('value')});"
            )
            output.append(statement + remainder + trailing + comment + newline)
        else:
            output.append(line)
        offset += len(line)

    return "".join(output)


def translate_bracket_assignments(source: str) -> str:
    """Unpack a value into existing lvalues, including after loop macros."""
    comments = comment_mask(source)
    output: list[str] = []
    offset = 0

    for line in source.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]

        comment_at = len(body)
        for index in range(len(body) - 1):
            if body[index : index + 2] == "//" and comments[offset + index]:
                comment_at = index
                break
        statement = body[:comment_at]
        comment = body[comment_at:]
        trailing = statement[len(statement.rstrip()) :]
        statement = statement.rstrip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()

        mask = code_mask(statement)
        stack: list[int] = []
        pairs: list[tuple[int, int]] = []
        for index, character in enumerate(statement):
            if not mask[index]:
                continue
            if character == "[":
                stack.append(index)
            elif character == "]" and stack:
                pairs.append((stack.pop(), index))

        replacement: str | None = None
        for opening, closing in sorted(pairs):
            equals = closing + 1
            while equals < len(statement) and statement[equals] in " \t":
                equals += 1
            if equals >= len(statement) or statement[equals] != "=":
                continue
            if statement.startswith("==", equals):
                continue

            targets = [
                target.strip()
                for target in split_top_level_commas(
                    statement[opening + 1 : closing]
                )
            ]
            if len(targets) < 2 or not all(targets):
                continue
            prefix = statement[:opening]
            if re.search(r"\b(?:let|auto)[ \t]*$", prefix):
                continue
            value = statement[equals + 1 :].strip()
            if not value:
                continue

            requirements = " ".join(
                f"__u[{index}];" for index in range(len(targets))
            )
            indexed_assignments = " ".join(
                f"{target} = __u[{index}];"
                for index, target in enumerate(targets)
            )
            tuple_assignments = " ".join(
                f"{target} = std::get<{index}>(__u);"
                for index, target in enumerate(targets)
            )
            replacement = (
                f"{prefix}([&](auto&& __u) {{ "
                f"if constexpr (requires {{ {requirements} }}) {{ "
                f"{indexed_assignments} "
                f"}} else {{ {tuple_assignments} }} "
                f"}})({value})"
            )
            break

        if replacement is None:
            output.append(line)
        else:
            output.append(replacement + ";" + trailing + comment + newline)
        offset += len(line)

    return "".join(output)


def translate_multiple_let_declarations(source: str) -> str:
    """Translate `let x, y = a, b` into a structured binding."""
    code_comments = comment_mask(source)
    declaration = re.compile(
        r"^(?P<indent>[ \t]*)let[ \t]+"
        r"(?P<names>[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)+)"
        r"[ \t]*=[ \t]*(?P<values>.+?)$"
    )

    output: list[str] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]

        comment_at = len(body)
        for index in range(len(body) - 1):
            if body[index : index + 2] == "//" and code_comments[offset + index]:
                comment_at = index
                break

        statement = body[:comment_at]
        comment = body[comment_at:]
        trailing = statement[len(statement.rstrip()) :]
        statement = statement.rstrip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()

        match = declaration.match(statement)
        if match:
            statement = (
                f"{match.group('indent')}auto [{match.group('names')}] = "
                f"std::tuple{{{match.group('values')}}};"
            )
            output.append(statement + trailing + comment + newline)
        else:
            output.append(line)
        offset += len(line)

    return "".join(output)


def comment_mask(source: str) -> list[bool]:
    """Return a mask that is true only for characters inside comments."""
    mask = [False] * len(source)
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            end = source.find("\n", i)
            if end == -1:
                end = len(source)
            for j in range(i, end):
                mask[j] = True
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) if end == -1 else end + 2
            for j in range(i, end):
                mask[j] = True
            i = end
        elif source[i] in {'"', "'"}:
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            i += 1
    return mask


def insert_line_semicolons(source: str) -> str:
    """Insert statement semicolons at newlines and consume explicit `\\`."""
    lines = source.splitlines(keepends=True)
    comments = comment_mask(source)
    code = code_mask(source)

    # A lambda expression is a statement/expression, so a lambda assigned to a
    # variable still needs a semicolon after its closing brace. Remember the
    # braces that close lambdas separately from ordinary block braces; adding
    # `;` after every `}` would also alter functions, loops, and `if`s.
    lambda_header = re.compile(
        r"(?:^|[=(:,?;]|\breturn[ \t]+)[ \t]*"
        r"\[[^\]\r\n]*\][ \t]*"
        r"(?:<[^>{}\r\n]*>[ \t]*)?"
        r"(?:\([^{}\r\n]*\)[ \t]*)?"
        r"(?:(?:mutable|constexpr|consteval|static)[ \t]*)*"
        r"(?:noexcept(?:[ \t]*\([^{}\r\n]*\))?[ \t]*)?"
        r"(?:->[^{}\r\n]+)?$"
    )
    lambda_openings: set[int] = set()
    for position, character in enumerate(source):
        if character != "{" or not code[position]:
            continue
        line_start = max(
            source.rfind("\n", 0, position),
            source.rfind("\r", 0, position),
        ) + 1
        prefix = "".join(
            source[index] if code[index] else " "
            for index in range(line_start, position)
        )
        if lambda_header.search(prefix):
            lambda_openings.add(position)

    expression_brace_openings: set[int] = set(lambda_openings)
    comma_list_openings: set[int] = set()
    vector_initializer = re.compile(
        r"\b(?:vec|vector)(?:[ \t]*<[^{}\r\n]*>)?[ \t]*$"
    )
    function_block = re.compile(
        r"\)[ \t]*(?:(?:const|noexcept|override|final)[ \t]*)*"
        r"(?:->[^{}\r\n]+)?$"
    )
    non_expression_block = re.compile(
        r"(?:^|[;}])[ \t]*(?:else|do|try|namespace\b[^{}]*)[ \t]*$"
    )
    for position, character in enumerate(source):
        if character != "{" or not code[position]:
            continue
        line_start = max(
            source.rfind("\n", 0, position),
            source.rfind("\r", 0, position),
        ) + 1
        prefix = "".join(
            source[index] if code[index] else " "
            for index in range(line_start, position)
        )
        stripped_prefix = prefix.rstrip()
        previous = stripped_prefix[-1:] if stripped_prefix else ""
        is_expression = vector_initializer.search(prefix) or (
            stripped_prefix
            and previous != ")"
            and not function_block.search(stripped_prefix)
            and not non_expression_block.search(stripped_prefix)
        )
        if is_expression:
            expression_brace_openings.add(position)
            if (
                position not in lambda_openings
                and not re.match(r"[ \t]*(?:class|struct|union)\b", stripped_prefix)
            ):
                comma_list_openings.add(position)

    expression_brace_closings: set[int] = set()
    comma_list_spans: list[tuple[int, int]] = []
    brace_stack: list[int] = []
    for position, character in enumerate(source):
        if not code[position]:
            continue
        if character == "{":
            brace_stack.append(position)
        elif character == "}" and brace_stack:
            opening = brace_stack.pop()
            if opening in expression_brace_openings:
                expression_brace_closings.add(position)
            if opening in comma_list_openings:
                comma_list_spans.append((opening, position))

    # A statement may end immediately before a block's closing brace on the
    # same physical line (`if (ok) { work(); break}`).  Newline insertion
    # cannot see that boundary, so terminate it here.  Expression braces
    # (initializers and lambdas) are deliberately excluded.
    inline_insertions: list[int] = []
    for closing, character in enumerate(source):
        if character != "}" or not code[closing]:
            continue
        if closing in expression_brace_closings:
            continue
        previous = closing - 1
        while previous >= 0 and (
            source[previous].isspace() or not code[previous]
        ):
            previous -= 1
        if previous >= 0 and source[previous] not in ";{}:":
            inline_insertions.append(closing)
    if inline_insertions:
        for position in reversed(inline_insertions):
            source = source[:position] + ";" + source[position:]
        return insert_line_semicolons(source)

    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    bounds: list[tuple[int | None, int | None]] = []
    code_text: list[str] = []
    for line, start in zip(lines, offsets):
        body_length = len(line.rstrip("\r\n"))
        indices = [
            i
            for i in range(body_length)
            if not comments[start + i] and not line[i].isspace()
        ]
        first = indices[0] if indices else None
        last = indices[-1] if indices else None
        bounds.append((first, last))
        code_text.append(
            "".join(
                line[i]
                for i in range(body_length)
                if not comments[start + i]
            ).strip()
        )

    def next_code_line(index: int) -> str:
        for candidate in code_text[index + 1 :]:
            if candidate:
                return candidate
        return ""

    control_start = re.compile(
        r"^(?:else[ \t]+)?(?:if|for|while|switch|catch)\b[ \t]*\("
    )
    declaration_header = re.compile(r"^(?:class|struct|union|enum|namespace)\b")
    function_header = re.compile(
        r"\)\s*(?:(?:const|noexcept|override|final)\s*)*(?:->[^{]+)?$"
    )

    def is_control_header(code: str) -> bool:
        """Return true only when a line contains a bare control header."""
        match = control_start.match(code)
        if not match:
            return False
        depth = 0
        quote: str | None = None
        escaped = False
        for position in range(match.end() - 1, len(code)):
            character = code[position]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character in {'"', "'"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return not code[position + 1 :].strip()
        return False

    output: list[str] = []
    for index, line in enumerate(lines):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n") or line.endswith("\r"):
            body, newline = line[:-1], line[-1]

        first, last = bounds[index]
        if not newline or first is None or last is None:
            output.append(line)
            continue

        if body[first] == "#":
            output.append(line)
            continue

        if body[last] == "\\":
            output.append(body[:last] + body[last + 1 :] + newline)
            continue

        code = code_text[index]
        last_character = body[last]
        absolute_last = offsets[index] + last
        closes_expression = absolute_last in expression_brace_closings
        inside_comma_list = any(
            opening < absolute_last < closing
            for opening, closing in comma_list_spans
        )
        skip = last_character in ";{}:" and not closes_expression
        skip = skip or inside_comma_list
        skip = skip or code.startswith("template")
        skip = skip or code in {"else", "do", "try"}
        skip = skip or is_control_header(code)

        following = next_code_line(index)
        if following.startswith("{"):
            skip = skip or bool(declaration_header.match(code))
            skip = skip or bool(function_header.search(code))
            skip = skip or code.endswith("]")

        if skip:
            output.append(line)
        else:
            output.append(body[: last + 1] + ";" + body[last + 1 :] + newline)

    return "".join(output)


def wrap_solve_if_missing(source: str) -> str:
    """Treat a program without a solve definition as the body of solve()."""
    solve_definition = re.compile(r"\bvoid[ \t\r\n]+solve[ \t\r\n]*"
                                  r"\([ \t\r\n]*\)[ \t\r\n]*\{")
    mask = code_mask(source)
    for match in solve_definition.finditer(source):
        if mask[match.start()]:
            return source

    # In implicit-solve mode, treat EOF as a newline as well. This guarantees
    # that the final statement receives its semicolon even when the file has no
    # terminating newline. Re-running the pass is safe because existing
    # semicolons are left unchanged.
    body = source.rstrip("\r\n")
    if body:
        body = insert_line_semicolons(body + "\n").rstrip("\r\n")
    body = body.rstrip()
    if body:
        body += "\n"
    return 'void solve() {\n#line 1 "main.ecpp"\n' + body + "}"


def translate_solve_separator(source: str) -> str:
    """Make the part after a standalone `---` line the body of solve()."""
    separator = re.compile(r"(?m)^[ \t]*---[ \t]*(?:\r?\n|$)")
    mask = code_mask(source)
    matches = [match for match in separator.finditer(source) if mask[match.start()]]
    if not matches:
        return source
    if len(matches) != 1:
        raise TranslationError("main.ecpp may contain only one `---` solve separator")

    solve_definition = re.compile(
        r"\bvoid[ \t\r\n]+solve[ \t\r\n]*\([ \t\r\n]*\)[ \t\r\n]*\{"
    )
    for match in solve_definition.finditer(source):
        if mask[match.start()]:
            raise TranslationError(
                "`---` solve separator cannot be combined with a solve definition"
            )

    match = matches[0]
    prefix = source[: match.start()]
    suffix = source[match.end() :]
    suffix_line = source.count("\n", 0, match.end()) + 1
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    body = suffix.rstrip()
    if body:
        body += "\n"
    return (
        prefix
        + "void solve() {\n"
        + f'#line {suffix_line} "main.ecpp"\n'
        + body
        + "}"
    )


def translate_global_type_aliases(source: str) -> str:
    """Infer global type aliases from `S=ll` and `(S:=ll)` prefixes."""
    if not re.search(r"\bvoid[ \t\r\n]+solve[ \t\r\n]*\(", source):
        return source
    known = set(BUILTIN_TYPE_NAMES)
    known.update(declared_type_names(source))
    inline = re.compile(
        r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*"
        r"(?P<type>[A-Za-z_]\w*(?:::\w+)*(?:[ \t]*<[^;={}()]+>)?)[ \t]*;"
        r"(?P<rest>[^\r\n]*)(?P<newline>\r?\n)?$"
    )
    plain = re.compile(
        r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*"
        r"(?P<type>[A-Za-z_]\w*(?:::\w+)*(?:[ \t]*<[^;={}()]+>)?)"
        r"(?P<tail>[ \t]*;?[ \t]*(?://.*)?(?:\r?\n)?$)"
    )
    walrus = re.compile(
        r"^(?P<indent>[ \t]*)\([ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*:=[ \t]*"
        r"(?P<type>[A-Za-z_]\w*(?:::\w+)*(?:[ \t]*<[^;={}()]+>)?)[ \t]*\)"
    )
    output: list[str] = []
    depth = 0
    for line in source.splitlines(keepends=True):
        current = line
        if depth == 0:
            match = inline.match(current)
            if match and match.group("type").split("::", 1)[0].strip() in known:
                current = (
                    f'{match.group("indent")}using {match.group("name")}='
                    f'{match.group("type")};{match.group("rest").lstrip()}'
                    f'{match.group("newline") or ""}'
                )
                known.add(match.group("name"))
            else:
                match = plain.match(current)
            if match and match.re is plain and match.group("type").split("::", 1)[0].strip() in known:
                newline = "\r\n" if current.endswith("\r\n") else "\n" if current.endswith("\n") else ""
                current = (
                    f'{match.group("indent")}using {match.group("name")}='
                    f'{match.group("type")};{newline}'
                )
                known.add(match.group("name"))
            elif not (match and match.re is inline):
                match = walrus.match(current)
                if match and match.group("type").split("::", 1)[0].strip() in known:
                    current = (
                        f'{match.group("indent")}using {match.group("name")}='
                        f'{match.group("type")};'
                        + current[match.end() :].lstrip()
                    )
                    known.add(match.group("name"))
        output.append(current)
        mask = code_mask(current)
        depth += sum(
            1 if character == "{" else -1 if character == "}" else 0
            for index, character in enumerate(current)
            if mask[index]
        )
    return "".join(output)


def make_dispatchers(names: set[str]) -> str:
    """Generate compact type-aware member dispatchers."""
    dispatchers = []
    for name in sorted(names):
        dispatchers.append(
            f"template<class S,class...A>decltype(auto)__d_{name}"
            f"(S&&s,A&&...a){{if constexpr(requires{{__x_{name}("
            "std::forward<S>(s),std::forward<A>(a)...);})return "
            f"__x_{name}(std::forward<S>(s),"
            "std::forward<A>(a)...);else if constexpr(requires{"
            f"__x_{name}(s,std::forward<A>(a)...);}})return "
            f"__x_{name}(s,std::forward<A>(a)...);else return "
            f"std::forward<S>(s).{name}(std::forward<A>(a)...);}}"
        )
    return "\n".join(dispatchers)


def compact_cpp_line(line: str) -> str:
    """Remove whitespace that is not needed to keep adjacent C++ tokens apart."""
    operator_pairs = {
        "++", "--", "->", "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=",
        "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "::", ".*", "##",
    }

    def needs_space(left: str, right: str) -> bool:
        word_left = left.isalnum() or left in "_$"
        word_right = right.isalnum() or right in "_$"
        return (
            (word_left and word_right)
            or (left.isdigit() and right == ".")
            or (left == "." and right.isdigit())
            or left + right in operator_pairs
            or (left == "/" and right in "/*")
        )

    output: list[str] = []
    pending_space = False
    index = 0
    while index < len(line):
        character = line[index]
        if character.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space and output and needs_space(output[-1][-1], character):
            output.append(" ")
        pending_space = False
        if character in {'"', "'"}:
            quote = character
            start = index
            index += 1
            while index < len(line):
                if line[index] == "\\":
                    index += 2
                elif line[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            output.append(line[start:index])
        else:
            output.append(character)
            index += 1
    return "".join(output)


def shorten_internal_identifiers(source: str) -> str:
    """Shorten translator-owned reserved identifiers without touching user text."""
    substitutions = [
        (r"__l(\d+)_end", r"__e\1"),
        (r"__l(\d+)_step", r"__s\1"),
    ]
    for pattern, replacement in substitutions:
        mask = code_mask(source)
        matches = [
            match for match in re.finditer(pattern, source) if mask[match.start()]
        ]
        for match in reversed(matches):
            value = match.expand(replacement)
            source = source[: match.start()] + value + source[match.end() :]
    return source


def minify_generated_cpp(source: str) -> str:
    """Join ordinary generated C++ lines while preserving directive boundaries."""
    output: list[str] = []
    pending: list[str] = []
    continued_directive = False

    def flush() -> None:
        if pending:
            output.append(compact_cpp_line(" ".join(pending)))
            pending.clear()

    for line in source.splitlines():
        stripped = line.strip()
        comments = comment_mask(line)
        has_line_comment = any(
            line.startswith("//", index) and comments[index]
            for index in range(max(0, len(line) - 1))
        )
        directive = stripped.startswith("#") or continued_directive
        if directive or stripped.startswith("//") or has_line_comment:
            flush()
            output.append(line.rstrip())
            continued_directive = directive and line.rstrip().endswith("\\")
        elif stripped:
            pending.append(compact_cpp_line(stripped))
            continued_directive = False
    flush()
    return "\n".join(output)


def specialize_local_mode(source: str, judge: bool | None) -> str:
    """Fold LOCAL/ONLINE_JUDGE branches when the output mode is known."""
    if judge is None:
        return source
    startup = re.compile(
        r"\A#ifndef ONLINE_JUDGE\s*\n#define LOCAL\s*\n"
        r"constexpr bool local=true;\s*\n#else\s*\n"
        r"(?P<submit>(?:#pragma[^\n]*\n)+)"
        r"constexpr bool local=false;\s*\n#endif\s*\n"
    )
    match = startup.match(source)
    if not match:
        raise TranslationError("could not specialize LOCAL startup block")
    submit_pragmas = (
        '_Pragma("GCC optimize(\\"Ofast,unroll-loops\\")") '
        '_Pragma("GCC target(\\"tune=native\\")")\n'
    )
    source = source[match.end() :]
    if not judge:
        source, count = re.subn(
            r"\A((?:#include[^\n]*\n)+)",
            lambda item: item.group(1) + submit_pragmas,
            source,
            count=1,
        )
        if count != 1:
            raise TranslationError("could not place submission pragmas")
    conditional = re.compile(
        r"(?ms)^#ifdef LOCAL[ \t]*\r?\n(?P<judge>.*?)"
        r"^#else[ \t]*\r?\n(?P<submit>.*?)^#endif[ \t]*(?:\r?\n|$)"
    )
    source, count = conditional.subn(
        lambda item: item.group("judge" if judge else "submit"), source
    )
    if count != 1:
        raise TranslationError("could not specialize LOCAL main block")
    if judge:
        source = source.replace("if constexpr(!local)return;", "")
    else:
        source = re.sub(
            r"template<class T,class\.\.\.Ts>void debug_\([^{}]*\)\{[^{}]*\}"
            r"template<class T,class\.\.\.Ts>void debug\([^{}]*\)\{[^{}]*\}",
            "template<class...T>void debug_(T&&...){}template<class...T>void debug(T&&...){}",
            source,
            count=1,
        )
    return source


def original_source_comment(source: str) -> str:
    """Render only the specification link and original source comments."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    commented_lines = []
    for line in lines:
        # A backslash immediately before a physical newline splices the next
        # line before C++ removes comments. Keep it away from the newline so
        # generated C++ can never become part of the comment.
        suffix = " " if line.endswith("\\") else ""
        commented_lines.append(f"// {line}{suffix}")

    return "// https://github.com/es142857/Extended-C-Specification-for-AtCoder\n" + "".join(
        line + "\n" for line in commented_lines
    )


def translate(
    preamble: Path,
    program: Path,
    *,
    include_source_comment: bool = True,
    include_line_directives: bool = True,
    judge_mode: bool | None = None,
) -> str:
    sources = {
        "preamble.ecpp": preamble.read_text(encoding="utf-8"),
        "main.ecpp": program.read_text(encoding="utf-8"),
    }
    sources["preamble.ecpp"], embedded_acl = extract_embedded_acl(
        sources["preamble.ecpp"]
    )
    record_preamble = translate_record_aliases(
        specialize_local_mode(sources["preamble.ecpp"], judge_mode)
    )
    record_main = translate_container_type_names(
        translate_record_aliases(
            translate_global_type_aliases(
                translate_solve_separator(
                    translate_line_continuations(sources["main.ecpp"])
                )
            )
        )
    )
    translated_preamble, extension_names = translate_declarations(record_preamble)
    translated_main, main_extension_names = translate_declarations(record_main)
    translated_preamble = translate_macro_syntax(translated_preamble)
    if main_extension_names:
        names = ", ".join(sorted(main_extension_names))
        raise TranslationError(
            "extension method declarations belong in preamble.ecpp "
            f"(found in main.ecpp: {names})"
        )

    type_names = declared_type_names(translated_preamble) | declared_type_names(
        translated_main
    )
    translated_main = split_top_level_statements(translated_main)
    translated_main = translate_string_literals(translated_main)
    translated_main = translate_inferred_for_loops(translated_main)
    translated_main = translate_lambda_parameter_types(translated_main, type_names)
    translated_main = translate_function_return_types(translated_main)
    translated_main = translate_function_parameter_types(translated_main, type_names)
    translated_main = translate_walrus_expressions(translated_main)
    translated_main = translate_chained_assignments(translated_main)
    translated_main = translate_slice_expressions(translated_main)
    translated_main = translate_pair_member_aliases(translated_main)
    translated_main = translate_inferred_pairs(translated_main)
    translated_main = translate_macro_syntax(translated_main)
    translated_main = translate_sort_calls(translated_main)
    translated_main = translate_reverse_calls(translated_main)
    translated_main = translate_bound_calls(translated_main)
    translated_main = translate_iota_calls(translated_main)
    translated_main = translate_vector_comprehensions(translated_main)
    implicit_main = translate_implicit_let_declarations(
        translated_main,
        type_names,
    )
    implicit_main = translate_function_map_calls(implicit_main)
    implicit_main = translate_while_conditions(implicit_main)

    main_body = translate_casts(
        translate_let_declarations(
            translate_calls(implicit_main, extension_names)
        )
    )
    main_body = insert_line_semicolons(main_body)
    main_body = translate_function_implicit_returns(main_body, type_names)
    main_body = translate_lambda_implicit_returns(main_body, type_names)

    translated = {
        "preamble.ecpp": translate_casts(
            translate_let_declarations(translated_preamble)
        ),
        "main.ecpp": wrap_solve_if_missing(main_body),
    }
    if embedded_acl is None:
        translated["preamble.ecpp"] = embed_atcoder_library(
            translated["preamble.ecpp"], preamble.parent / "ac-library"
        )
    else:
        translated["preamble.ecpp"] = inject_embedded_acl(
            translated["preamble.ecpp"], embedded_acl
        )

    source_comment = (
        original_source_comment(sources["main.ecpp"])
        if include_source_comment
        else ""
    )
    generated = (
        '#line 1 "preamble.ecpp"\n'
        + translated["preamble.ecpp"].rstrip()
        + "\n"
        + make_dispatchers(extension_names)
        + "\n"
        + '#line 1 "main.ecpp"\n'
        + translated["main.ecpp"].rstrip()
        + "\n"
    )
    if not include_line_directives:
        generated = re.sub(
            r'(?m)^[ \t]*#line[^\r\n]*(?:\r?\n|$)', "", generated
        )
    return source_comment + shorten_internal_identifiers(minify_generated_cpp(generated))


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate Extended C++ to C++")
    parser.add_argument("preamble", type=Path)
    parser.add_argument("program", type=Path)
    parser.add_argument("output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--judge", action="store_true")
    mode.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    try:
        output = translate(
            args.preamble,
            args.program,
            include_source_comment=not args.judge,
            include_line_directives=not args.submit,
            judge_mode=True if args.judge else False if args.submit else None,
        )
        args.output.write_text(output, encoding="utf-8")
    except (OSError, TranslationError) as error:
        print(f"ecpp: error: {error}", file=sys.stderr)
        return 1
    print(f"Translated {args.program.name} -> {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
























