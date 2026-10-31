#!/usr/bin/env python3
"""Extended C++ (.ecpp) to C++ source translator."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DECLARATION = re.compile(
    r"(?m)^(?P<indent>[ \t]*)"
    r"(?P<return>[A-Za-z_~][\w:<>,*& \t]*?)"
    r"[ \t]+additional[ \t]+"
    r"(?P<receiver>[A-Za-z_][\w:<>,*& \t]*?)"
    r"[ \t]*\.[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*"
    r"\((?P<arguments>[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
    r"(?P<const>[ \t]+const)?"
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


def translate_declarations(source: str) -> tuple[str, set[str]]:
    mask = code_mask(source)
    matches = []
    for match in DECLARATION.finditer(source):
        keyword_at = source.find("additional", match.start(), match.end())
        if keyword_at >= 0 and mask[keyword_at]:
            matches.append(match)

    names: set[str] = set()
    for match in reversed(matches):
        name = match.group("name")
        names.add(name)
        return_type = match.group("return").strip()
        receiver = match.group("receiver").strip()
        arguments = match.group("arguments").strip()
        is_const = bool(match.group("const"))
        self_type = f"const {receiver}&" if is_const else f"{receiver}&"
        parameters = f"{self_type} self"
        if arguments and arguments != "void":
            parameters += f", {arguments}"
        replacement = (
            f"{match.group('indent')}{return_type} "
            f"__ecpp_additional_{name}({parameters})"
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
            break
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
        forwarded_receiver = (
            "std::forward<decltype(__ecpp_receiver)>(__ecpp_receiver)"
        )
        if (
            name in {"append", "appendleft"}
            and arguments.startswith("{")
            and arguments.endswith("}")
        ):
            arguments = (
                "typename std::remove_cvref_t<decltype(__ecpp_receiver)>"
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
                    "[&](auto&& __ecpp_map_value) -> decltype(auto) { "
                    f"auto&& {placeholder_name} = "
                    "std::forward<decltype(__ecpp_map_value)>("
                    "__ecpp_map_value); return "
                    f"({arguments}); }}"
                )
            elif zero_argument_lambda:
                fallback = (
                    "[&](auto&& __ecpp_map_value) -> decltype(auto) { "
                    "(void)__ecpp_map_value; return "
                    f"({arguments})(); }}"
                )
            else:
                fallback = (
                    "[&](auto&& __ecpp_map_value) -> decltype(auto) { "
                    "if constexpr (requires { "
                    f"({arguments})(std::forward<decltype(__ecpp_map_value)>("
                    "__ecpp_map_value)); }) { return "
                    f"({arguments})(std::forward<decltype(__ecpp_map_value)>("
                    "__ecpp_map_value)); } else { return "
                    f"({arguments}); }} }}"
                )
            if uses_element_placeholder:
                replacement = (
                    "([&](auto&& __ecpp_receiver) -> decltype(auto) { return "
                    f"__ecpp_dispatch_{name}("
                    f"{forwarded_receiver}, {fallback}); "
                    f"}})({receiver})"
                )
            else:
                replacement = (
                    "([&](auto&& __ecpp_receiver) -> decltype(auto) { "
                    f"if constexpr (requires {{ __ecpp_additional_{name}("
                    f"__ecpp_receiver, {arguments}); }}) {{ "
                    f"return __ecpp_dispatch_{name}({call_args}); "
                    f"}} else {{ return __ecpp_dispatch_{name}("
                    f"{forwarded_receiver}, {fallback}); }} "
                    f"}})({receiver})"
                )
        else:
            replacement = (
                "([&](auto&& __ecpp_receiver) -> decltype(auto) { return "
                f"__ecpp_dispatch_{name}({call_args}); "
                f"}})({receiver})"
            )
        source = source[:start] + replacement + source[closing + 1 :]


def translate_casts(source: str) -> str:
    """Translate Extended C++ functional casts to explicit C-style casts."""
    cast = re.compile(r"\b(?P<type>int|str|bool|ld)[ \t]*\(")
    mask = code_mask(source)
    matches = [match for match in cast.finditer(source) if mask[match.start()]]
    for match in reversed(matches):
        opening = match.end() - 1
        closing = find_closing_paren(source, opening, mask)
        if not source[opening + 1 : closing].strip():
            continue
        replacement = f"({match.group('type')})("
        source = source[: match.start()] + replacement + source[match.end() :]
    return source


def translate_pair_member_aliases(source: str) -> str:
    """Translate pair `.f` and `.s` aliases to `.first` and `.second`."""
    mask = code_mask(source)
    alias = re.compile(r"\.[ \t]*(?P<name>f|s)\b")
    replacements = []
    for match in alias.finditer(source):
        if not mask[match.start()]:
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
    """Translate Python-style vector slices into `__ecpp_slice` calls."""
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
        replacement = f"__ecpp_slice({receiver}, {start}, {stop}, {step})"
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
    """Hoist `name := expression` while leaving its assigned value in place."""
    walrus = re.compile(r"\b(?P<name>[A-Za-z_]\w*)[ \t]*:=[ \t]*")
    output: list[str] = []

    for line in source.splitlines(keepends=True):
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            body, newline = line[:-1], line[-1]
        else:
            body, newline = line, ""

        assignments: list[tuple[str, str]] = []
        while True:
            mask = code_mask(body)
            matches = [match for match in walrus.finditer(body) if mask[match.start()]]
            if not matches:
                break

            candidates: list[tuple[re.Match[str], int]] = []
            for match in matches:
                end = walrus_rhs_end(body, match.end(), mask)
                nested = any(
                    match.end() <= other.start() < end
                    for other in matches
                    if other is not match
                )
                if not nested:
                    candidates.append((match, end))
            if not candidates:
                raise TranslationError("could not determine walrus expression nesting")

            match, end = candidates[0]
            expression = body[match.end() : end].strip()
            if not expression:
                raise TranslationError(
                    f"missing expression after walrus assignment to {match.group('name')}"
                )
            name = match.group("name")
            assignments.append((name, expression))
            body = body[: match.start()] + name + body[end:]

        if assignments:
            indent = re.match(r"[ \t]*", body).group()
            for name, expression in assignments:
                output.append(f"{indent}{name} = {expression}{newline or chr(10)}")
        output.append(body + newline)

    return "".join(output)


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

    for line in source.splitlines(keepends=True):
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
            if is_last:
                output.append(assignment + suffix + trailing + comment + newline)
            else:
                output.append(assignment + suffix + (newline or "\n"))
        offset += len(line)

    return "".join(output)


def split_top_level_statements(source: str) -> str:
    """Put same-line statements on separate lines before declaration inference."""
    mask = code_mask(source)
    output: list[str] = []
    offset = 0

    for line in source.splitlines(keepends=True):
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
            output.append(line)
            offset += len(line)
            continue

        indent = re.match(r"[ \t]*", body).group()
        start = 0
        for separator in separators:
            statement = body[start : separator + 1].strip()
            if statement:
                output.append(indent + statement + (newline or "\n"))
            start = separator + 1
        remainder = body[start:].strip()
        if remainder:
            output.append(indent + remainder + newline)
        offset += len(line)

    return "".join(output)


def translate_inferred_for_loops(source: str) -> str:
    """Infer `auto` for omitted range-for declarations."""
    binding = re.compile(
        r"\bfor[ \t]*\([ \t]*(?P<bracket>\["
        r"[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*\])"
        r"[ \t]*:"
    )
    variable = re.compile(
        r"\bfor[ \t]*\([ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*:"
    )
    mask = code_mask(source)
    insertions = {
        match.start("bracket")
        for match in binding.finditer(source)
        if mask[match.start()]
    }
    insertions.update(
        match.start("name")
        for match in variable.finditer(source)
        if mask[match.start()]
    )
    for position in sorted(insertions, reverse=True):
        source = source[:position] + "auto " + source[position:]
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
            f"__ecpp_vector_comprehension{template_arguments}("
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
        replacement = f"__ecpp_iota{match.group('spacing')}("
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
        insertions.append((opening + 1, f"__ecpp_rep{counter}, "))
        counter += 1

    for position, insertion in reversed(insertions):
        source = source[:position] + insertion + source[position:]
    return source


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
    "pair",
    "priority_queue",
    "short",
    "signed",
    "size_t",
    "str",
    "tuple",
    "unsigned",
    "vec",
    "vector",
    "void",
}


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


def translate_lambda_parameter_types(source: str, type_names: set[str]) -> str:
    """Add `auto` to bare parameters in lambda expressions."""
    mask = code_mask(source)
    replacements: list[tuple[int, int, str]] = []

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
        while position < len(source) and source[position] in " \t\r\n":
            position += 1
        if position >= len(source) or source[position] != "(":
            continue

        parameters_open = position
        parameters_close = find_closing_paren(source, parameters_open, mask)
        suffix = source[parameters_close + 1 :]
        lambda_suffix = re.match(
            r"\s*(?:(?:mutable|constexpr|consteval|static)\s+)*"
            r"(?:noexcept(?:\s*\([^()\n]*\))?\s*)?"
            r"(?:->[^{;\n]+\s*)?(?:requires[^{;\n]+\s*)?\{",
            suffix,
        )
        if not lambda_suffix:
            continue
        body_open = parameters_close + 1 + lambda_suffix.end() - 1
        if body_open >= len(mask) or not mask[body_open]:
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

    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
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
                f"__ecpp_unpack[{index}];" for index in range(len(names))
            )
            elements = ", ".join(
                f"__ecpp_unpack[{index}]" for index in range(len(names))
            )
            statement = (
                f"{match.group('indent')}auto [{match.group('names')}] = "
                "([&](auto&& __ecpp_unpack) { "
                f"if constexpr (requires {{ {requirements} }}) {{ "
                f"return std::tuple{{{elements}}}; "
                "} else { return std::forward<decltype(__ecpp_unpack)>("
                "__ecpp_unpack); } "
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
                f"__ecpp_unpack[{index}];" for index in range(len(targets))
            )
            indexed_assignments = " ".join(
                f"{target} = __ecpp_unpack[{index}];"
                for index, target in enumerate(targets)
            )
            tuple_assignments = " ".join(
                f"{target} = std::get<{index}>(__ecpp_unpack);"
                for index, target in enumerate(targets)
            )
            replacement = (
                f"{prefix}([&](auto&& __ecpp_unpack) {{ "
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
    vector_initializer = re.compile(
        r"\b(?:vec|vector)(?:[ \t]*<[^{}\r\n]*>)?[ \t]*$"
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
        if vector_initializer.search(prefix):
            expression_brace_openings.add(position)

    expression_brace_closings: set[int] = set()
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
        closes_expression = offsets[index] + last in expression_brace_closings
        skip = last_character in ";{}:" and not closes_expression
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


def make_dispatchers(names: set[str]) -> str:
    """Generate compact type-aware member dispatchers."""
    dispatchers = []
    for name in sorted(names):
        dispatchers.append(
            f"template<class S,class...A>decltype(auto)__ecpp_dispatch_{name}"
            f"(S&&s,A&&...a){{if constexpr(requires{{__ecpp_additional_{name}("
            "std::forward<S>(s),std::forward<A>(a)...);})return "
            f"__ecpp_additional_{name}(std::forward<S>(s),"
            "std::forward<A>(a)...);else if constexpr(requires{"
            f"__ecpp_additional_{name}(s,std::forward<A>(a)...);}})return "
            f"__ecpp_additional_{name}(s,std::forward<A>(a)...);else return "
            f"std::forward<S>(s).{name}(std::forward<A>(a)...);}}"
        )
    return "\n".join(dispatchers)


def find_untranslated_keyword(source: str) -> int | None:
    mask = code_mask(source)
    for match in re.finditer(r"\badditional\b", source):
        if mask[match.start()]:
            return match.start()
    return None


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


def translate(preamble: Path, program: Path) -> str:
    sources = {
        "preamble.ecpp": preamble.read_text(encoding="utf-8"),
        "main.ecpp": program.read_text(encoding="utf-8"),
    }
    record_preamble = translate_record_aliases(sources["preamble.ecpp"])
    record_main = translate_record_aliases(sources["main.ecpp"])
    translated_preamble, extension_names = translate_declarations(record_preamble)
    translated_main, main_extension_names = translate_declarations(record_main)
    if main_extension_names:
        names = ", ".join(sorted(main_extension_names))
        raise TranslationError(
            "additional declarations belong in preamble.ecpp "
            f"(found in main.ecpp: {names})"
        )

    type_names = declared_type_names(translated_preamble) | declared_type_names(
        translated_main
    )
    translated_main = split_top_level_statements(translated_main)
    translated_main = translate_inferred_for_loops(translated_main)
    translated_main = translate_lambda_parameter_types(translated_main, type_names)
    translated_main = translate_walrus_expressions(translated_main)
    translated_main = translate_chained_assignments(translated_main)
    translated_main = translate_slice_expressions(translated_main)
    translated_main = translate_pair_member_aliases(translated_main)
    translated_main = translate_rep_shorthand(translated_main)
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

    translated = {
        "preamble.ecpp": translate_casts(
            translate_let_declarations(translated_preamble)
        ),
        "main.ecpp": wrap_solve_if_missing(
            insert_line_semicolons(
                translate_casts(
                    translate_let_declarations(
                        translate_calls(implicit_main, extension_names)
                    )
                )
            )
        ),
    }

    for name in translated:
        bad = find_untranslated_keyword(translated[name])
        if bad is not None:
            line = translated[name].count("\n", 0, bad) + 1
            raise TranslationError(
                f"{name}:{line}: 'additional' declaration could not be parsed"
            )

    return (
        original_source_comment(sources["main.ecpp"])
        + '#line 1 "preamble.ecpp"\n'
        + translated["preamble.ecpp"].rstrip()
        + "\n"
        + make_dispatchers(extension_names)
        + "\n"
        + '#line 1 "main.ecpp"\n'
        + translated["main.ecpp"].rstrip()
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate Extended C++ to C++")
    parser.add_argument("preamble", type=Path)
    parser.add_argument("program", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        output = translate(args.preamble, args.program)
        args.output.write_text(output, encoding="utf-8")
    except (OSError, TranslationError) as error:
        print(f"ecpp: error: {error}", file=sys.stderr)
        return 1
    print(f"Translated {args.program.name} -> {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
