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
        call_args = (
            forwarded_receiver
            if not arguments
            else f"{forwarded_receiver}, {arguments}"
        )
        # In a native member call, the receiver is evaluated before the
        # arguments.  A direct rewrite to a free function would lose that
        # guarantee because free-function argument order is unspecified.
        if name == "map" and arguments:
            argument_mask = code_mask(arguments)
            argument_is_lambda = arguments.lstrip().startswith("[")
            uses_element_placeholder = not argument_is_lambda and any(
                argument_mask[placeholder.start()]
                for placeholder in re.finditer(
                    r"(?<![\w.:>])th\b", arguments
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
                    "auto&& th = std::forward<decltype(__ecpp_map_value)>("
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
                    "__ecpp_dispatch_map("
                    f"{forwarded_receiver}, {fallback}); "
                    f"}})({receiver})"
                )
            else:
                replacement = (
                    "([&](auto&& __ecpp_receiver) -> decltype(auto) { "
                    "if constexpr (requires { __ecpp_additional_map("
                    f"__ecpp_receiver, {arguments}); }}) {{ "
                    f"return __ecpp_dispatch_map({call_args}); "
                    "} else { return __ecpp_dispatch_map("
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


def binding_names(code: str) -> list[str]:
    """Return names introduced by a leading `let` or `auto`."""
    match = re.match(r"^[ \t]*(?:let|auto)[ \t]+\[([^\]\n]+)\]", code)
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


def translate_implicit_let_declarations(
    source: str, type_names: set[str]
) -> str:
    """Prefix an assignment to a not-yet-declared leading name with `let`."""
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

        list_match = re.match(
            r"^(?P<indent>[ \t]*)\["
            r"(?P<names>[A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)+)"
            r"\][ \t]*=(?!=)",
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
        elif name_match and not is_defined(name_match.group("name")):
            inserted_names = [name_match.group("name")]
            indent_length = len(name_match.group("indent"))

        if inserted_names:
            body = body[:indent_length] + "let " + body[indent_length:]
            code = code[:indent_length] + "let " + code[indent_length:]
            scopes[-1].update(inserted_names)

        scopes[-1].update(binding_names(code))
        scopes[-1].update(explicit_declaration_names(code, type_names))
        block_names: set[str] = set()
        for loop_match in re.finditer(
            r"\b(?:rep|rrep|forl)[ \t]*\([ \t]*([A-Za-z_]\w*)[ \t]*,",
            code,
        ):
            block_names.add(loop_match.group(1))
        for loop_match in re.finditer(
            r"\bfor[ \t]*\([^;:\n]*\b([A-Za-z_]\w*)[ \t]*(?:=|:)",
            code,
        ):
            block_names.add(loop_match.group(1))

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
    source = translate_multiple_let_declarations(source)
    declaration = re.compile(
        r"\blet(?=[ \t]+(?:[A-Za-z_]\w*|\[[^\]\n]+\])"
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
        r"^(?P<indent>[ \t]*)let[ \t]+\["
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

    lambda_closings: set[int] = set()
    brace_stack: list[int] = []
    for position, character in enumerate(source):
        if not code[position]:
            continue
        if character == "{":
            brace_stack.append(position)
        elif character == "}" and brace_stack:
            opening = brace_stack.pop()
            if opening in lambda_openings:
                lambda_closings.add(position)

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
        closes_lambda = offsets[index] + last in lambda_closings
        skip = last_character in ";{}:" and not closes_lambda
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
    """Generate type-aware dispatchers that preserve unrelated member calls."""
    dispatchers = []
    for name in sorted(names):
        dispatchers.append(
            f"""template <class EcppSelf, class... EcppArgs>
decltype(auto) __ecpp_dispatch_{name}(EcppSelf&& self, EcppArgs&&... args) {{
    if constexpr (requires {{
        __ecpp_additional_{name}(
            std::forward<EcppSelf>(self),
            std::forward<EcppArgs>(args)...
        );
    }}) {{
        return __ecpp_additional_{name}(
            std::forward<EcppSelf>(self),
            std::forward<EcppArgs>(args)...
        );
    }} else if constexpr (requires {{
        __ecpp_additional_{name}(
            self,
            std::forward<EcppArgs>(args)...
        );
    }}) {{
        return __ecpp_additional_{name}(
            self,
            std::forward<EcppArgs>(args)...
        );
    }} else {{
        return std::forward<EcppSelf>(self).{name}(
            std::forward<EcppArgs>(args)...
        );
    }}
}}"""
        )
    return "\n\n".join(dispatchers)


def find_untranslated_keyword(source: str) -> int | None:
    mask = code_mask(source)
    for match in re.finditer(r"\badditional\b", source):
        if mask[match.start()]:
            return match.start()
    return None


def original_source_comment(name: str, source: str) -> str:
    """Render an Extended C++ source file as a safe C++ line comment."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    commented_lines = []
    for line in lines:
        # A backslash immediately before a physical newline splices the next
        # line before C++ removes comments. Keep it away from the newline so
        # the generated-code marker can never become part of the comment.
        suffix = " " if line.endswith("\\") else ""
        commented_lines.append(f"// {line}{suffix}")

    return (
        "// https://github.com/es142857/Extended-C-Specification-for-AtCoder\n"
        f"// ----- Original Extended C++ source: {name} -----\n"
        + "\n".join(commented_lines)
        + f"\n// ----- End original Extended C++ source: {name} -----\n\n"
    )


def translate(preamble: Path, program: Path) -> str:
    sources = {
        "preamble.ecpp": preamble.read_text(encoding="utf-8"),
        "main.ecpp": program.read_text(encoding="utf-8"),
    }
    translated_preamble, extension_names = translate_declarations(
        sources["preamble.ecpp"]
    )
    translated_main, main_extension_names = translate_declarations(sources["main.ecpp"])
    if main_extension_names:
        names = ", ".join(sorted(main_extension_names))
        raise TranslationError(
            "additional declarations belong in preamble.ecpp "
            f"(found in main.ecpp: {names})"
        )

    translated_main = translate_iota_calls(translated_main)
    translated_main = translate_vector_comprehensions(translated_main)
    implicit_main = translate_implicit_let_declarations(
        translated_main, declared_type_names(sources["preamble.ecpp"])
    )

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
        original_source_comment(program.name, sources["main.ecpp"])
        + "// Generated by Extended C++. Edit preamble.ecpp or main.ecpp, not this file.\n"
        '#line 1 "preamble.ecpp"\n'
        + translated["preamble.ecpp"].rstrip()
        + "\n\n"
        + "// Type-aware Extended C++ member dispatch.\n"
        + make_dispatchers(extension_names)
        + "\n\n"
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
