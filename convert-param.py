#!/usr/bin/env python3
"""Convert Perl code in .param files to the date mini-language of file-transfer.py.

The Perl tool allowed `FileMask` and `FileCreationDate` to hold Perl code. The Python
tool uses date tokens instead (see docs/date-syntax.md). This script finds the .param
files that still contain Perl code and, after asking for each one, converts them.

For every converted file the original is first copied to <name>.param_perl.

Usage:
    convert-param.py [folder] [--dry-run] [--yes]

`folder` defaults to the current directory. Only the common Perl patterns are
understood (localtime, sprintf("%02d", $var+N), string concatenation, localtime(time-N));
anything else is reported and the file is left untouched.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

KEYS = ("FileMask", "FileCreationDate")
BACKUP_SUFFIX = "_perl"  # job.param -> job.param_perl
SEPARATORS = set("-_ :./T")  # characters allowed between the parts of a date block

LINE_RE = re.compile(
    r"^(?P<head>\s*(?P<key>FileMask|FileCreationDate)\s*:\s*)(?P<value>.*?)(?P<tail>[ \t]*(?:\r?\n)?)$"
)
PERL_HINT = re.compile(
    r"\blocaltime\b|\bsprintf\s*\(|^\s*my\b|;\s*(?:my\s+)?\$\w+\s*=|^\s*\$\w+\s*="
    r"|\b(?:if|unless|while|foreach|return)\s*\(|\bfor(?:each)?\s+my\b|\bsub\s*\{"
)

LOCALTIME_FIELDS = ["SS", "MI", "HH", "DD", "MM", "YYYY"]  # first six values of Perl's localtime
SHIFT_UNIT = {"YYYY": "y", "YY": "y", "MM": "mo", "DD": "d", "HH": "h", "MI": "mi", "SS": "s"}

Shift = Optional[Tuple[str, int]]  # (unit, amount) e.g. ("d", -1)


class Unsupported(Exception):
    """The Perl code uses something this converter does not understand."""


@dataclass(frozen=True)
class Raw:
    """A value straight from localtime(): month is 0-11, year is years since 1900."""
    unit: str
    base: Shift


@dataclass(frozen=True)
class Cal:
    """A calendar number (real month 1-12, real year) of the date shifted by `shift`."""
    unit: str
    shift: Shift


@dataclass(frozen=True)
class Part:
    """A zero-padded date part that is printed into the result."""
    unit: str
    shift: Shift


@dataclass
class Str:
    pieces: List[Union[str, Part]] = field(default_factory=list)


Value = Union[Raw, Cal, Str]


def add_shift(shift: Shift, unit: str, amount: int) -> Shift:
    if amount == 0:
        return shift
    if shift is None:
        return (unit, amount)
    if shift[0] != unit:
        raise Unsupported(f"mixes shifts of different units ({shift[0]} and {unit})")
    total = shift[1] + amount
    return (unit, total) if total else None


# --------------------------------------------------------------------------
# Perl subset interpreter
# --------------------------------------------------------------------------

def split_top_level(text: str, separator: str) -> List[str]:
    """Split on `separator`, ignoring it inside quotes and brackets."""
    parts: List[str] = []
    buffer: List[str] = []
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buffer.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buffer.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buffer.append(ch)
        elif ch in "([{":
            depth += 1
            buffer.append(ch)
        elif ch in ")]}":
            depth -= 1
            buffer.append(ch)
        elif ch == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(ch)
        i += 1
    if quote or depth:
        raise Unsupported("unbalanced quotes or brackets")
    parts.append("".join(buffer))
    return parts


def merge_literals(pieces: List[Union[str, Part]]) -> List[Union[str, Part]]:
    merged: List[Union[str, Part]] = []
    for piece in pieces:
        if isinstance(piece, str) and merged and isinstance(merged[-1], str):
            merged[-1] += piece
        elif not (isinstance(piece, str) and piece == ""):
            merged.append(piece)
    return merged


def raw_to_cal(raw: Raw, constant: int) -> Cal:
    """Raw localtime value + constant -> calendar number (undoes the +1 / +1900 corrections)."""
    if raw.unit == "YYYY":
        if 1890 <= constant <= 1910:
            return Cal("YYYY", add_shift(raw.base, "y", constant - 1900))
        if -110 <= constant <= -90:  # year-100 style two digit year
            return Cal("YY", add_shift(raw.base, "y", constant + 100))
        raise Unsupported(f"year calculation with constant {constant:+d} is not understood")
    if raw.unit == "MM":
        return Cal("MM", add_shift(raw.base, "mo", constant - 1))
    return Cal(raw.unit, add_shift(raw.base, SHIFT_UNIT[raw.unit], constant))


def numeric_value(text: str, variables: Dict[str, Value]) -> Cal:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    match = re.match(r"^\$(\w+)((?:\s*[+-]\s*\d+)*)$", text)
    if not match:
        raise Unsupported(f"numeric expression '{text}' is not understood")
    name = match.group(1)
    constant = sum(int(term.replace(" ", "")) for term in re.findall(r"[+-]\s*\d+", match.group(2)))
    value = variables.get(name)
    if isinstance(value, Str) and len(value.pieces) == 1 and isinstance(value.pieces[0], Part):
        value = Cal(value.pieces[0].unit, value.pieces[0].shift)
    if isinstance(value, Raw):
        return raw_to_cal(value, constant)
    if isinstance(value, Cal):
        return Cal(value.unit, add_shift(value.shift, SHIFT_UNIT[value.unit], constant))
    raise Unsupported(f"variable ${name} is not a date value")


def sprintf_part(text: str, variables: Dict[str, Value]) -> Part:
    match = re.match(r"^sprintf\s*\(\s*([\"'])%(0?)(\d*)d\1\s*,\s*(.+)\)$", text, re.S)
    if not match:
        raise Unsupported(f"'{text}' is not a sprintf(\"%02d\", $value) call")
    zero_padded, width = match.group(2) == "0", int(match.group(3) or 0)
    cal = numeric_value(match.group(4), variables)
    if cal.unit == "YYYY":
        ok = width <= 4
    else:
        ok = width == 2 and zero_padded
    if not ok:
        raise Unsupported(f"'{text}' pads the number differently than the date tokens do")
    return Part(cal.unit, cal.shift)


def interpolate(name: str, variables: Dict[str, Value]) -> List[Union[str, Part]]:
    value = variables.get(name)
    if value is None:
        raise Unsupported(f"variable ${name} is not defined")
    if isinstance(value, Str):
        return list(value.pieces)
    if isinstance(value, Cal) and value.unit == "YYYY":
        return [Part("YYYY", value.shift)]  # a bare year prints the same digits
    raise Unsupported(f"${name} is printed without zero padding")


def parse_double_quoted(body: str, variables: Dict[str, Value]) -> List[Union[str, Part]]:
    escapes = {"n": "\n", "t": "\t", "r": "\r"}
    pieces: List[Union[str, Part]] = []
    literal: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            literal.append(escapes.get(body[i + 1], body[i + 1]))  # Perl drops unknown backslashes
            i += 2
        elif ch == "$" and i + 1 == len(body):
            literal.append("$")
            i += 1
        elif ch == "$":
            match = re.match(r"\$(?:\{(\w+)\}|(\w+))", body[i:])
            if not match:
                raise Unsupported(f"cannot interpolate '{body[i:i + 12]}'")
            if literal:
                pieces.append("".join(literal))
                literal = []
            pieces.extend(interpolate(match.group(1) or match.group(2), variables))
            i += match.end()
        elif ch == "@":
            raise Unsupported("array interpolation")
        else:
            literal.append(ch)
            i += 1
    if literal:
        pieces.append("".join(literal))
    return pieces


def parse_single_quoted(body: str) -> List[Union[str, Part]]:
    return [re.sub(r"\\([\\'])", r"\1", body)]


def evaluate_expression(expr: str, variables: Dict[str, Value]) -> Value:
    expr = expr.strip()
    if re.match(r"^\(?\$\w+(?:\s*[+-]\s*\d+)+\)?$", expr):
        return numeric_value(expr, variables)
    operands = [operand.strip() for operand in split_top_level(expr, ".")]
    if len(operands) == 1 and re.match(r"^\$\w+$", operands[0]):
        value = variables.get(operands[0][1:])
        if value is None:
            raise Unsupported(f"variable {operands[0]} is not defined")
        return value
    pieces: List[Union[str, Part]] = []
    for operand in operands:
        if operand[:1] == '"' and operand[-1:] == '"' and len(operand) >= 2:
            pieces.extend(parse_double_quoted(operand[1:-1], variables))
        elif operand[:1] == "'" and operand[-1:] == "'" and len(operand) >= 2:
            pieces.extend(parse_single_quoted(operand[1:-1]))
        elif re.match(r"^\$\w+$", operand):
            pieces.extend(interpolate(operand[1:], variables))
        elif operand.startswith("sprintf"):
            pieces.append(sprintf_part(operand, variables))
        else:
            raise Unsupported(f"'{operand}' is not understood")
    return Str(merge_literals(pieces))


def seconds_to_shift(argument: str) -> Shift:
    """Translate the argument of localtime(time - 86400*5) to a shift."""
    argument = argument.strip()
    if not argument:
        return None
    if not argument.startswith("time") or not re.fullmatch(r"[\d\s*+\-()]*", argument[4:]):
        raise Unsupported(f"localtime({argument}) is not understood")
    try:
        seconds = int(eval("0" + argument[4:], {"__builtins__": {}}, {}))  # charset checked above
    except Exception:
        raise Unsupported(f"localtime({argument}) is not understood")
    if seconds == 0:
        return None
    for unit, size in (("d", 86400), ("h", 3600), ("mi", 60)):
        if seconds % size == 0:
            return (unit, seconds // size)
    return ("s", seconds)


def run_statement(statement: str, variables: Dict[str, Value]) -> Optional[Value]:
    """Execute one statement; returns the value of an assignment or bare expression."""
    match = re.match(r"^(?:my\s*)?\(([^)]*)\)\s*=\s*localtime\s*(?:\((.*)\))?$", statement, re.S)
    if match:
        base = seconds_to_shift(match.group(2) or "")
        for index, name in enumerate(n.strip() for n in match.group(1).split(",")):
            if name.startswith("$") and index < len(LOCALTIME_FIELDS):
                variables[name[1:]] = Raw(LOCALTIME_FIELDS[index], base)
            elif name.startswith("$"):
                raise Unsupported(f"{name} (weekday/day of year/dst) is not supported")
        return None
    if re.match(r"^my\s+\$\w+$", statement):
        return None
    match = re.match(r"^(?:my\s+)?\$(\w+)\s*=\s*(.+)$", statement, re.S)
    if match:
        value = evaluate_expression(match.group(2), variables)
        variables[match.group(1)] = value
        return value
    return evaluate_expression(statement, variables)


# --------------------------------------------------------------------------
# Rendering to the mini-language
# --------------------------------------------------------------------------

def block(fmt: str, shift: Shift) -> str:
    return "<" + fmt + (f"{shift[1]:+d}{shift[0]}" if shift else "") + ">"


def render_run(run: List[Union[str, Part]], notes: List[str]) -> str:
    parts = [item for item in run if isinstance(item, Part)]
    shifts = {part.shift for part in parts if part.shift}
    if len(shifts) > 1:
        notes.append("parts with different shifts were kept separate, so they are not corrected at month/year ends")
        return "".join(block(item.unit, item.shift) if isinstance(item, Part) else item for item in run)
    fmt = "".join(item.unit if isinstance(item, Part) else item for item in run)
    return block(fmt, shifts.pop() if shifts else None)


def render(pieces: List[Union[str, Part]], notes: List[str]) -> str:
    merged = merge_literals(pieces)
    output: List[str] = []
    i = 0
    while i < len(merged):
        item = merged[i]
        if isinstance(item, str):
            output.append(item)
            i += 1
            continue
        run: List[Union[str, Part]] = [item]
        j = i + 1
        while j < len(merged):
            if isinstance(merged[j], Part):
                run.append(merged[j])
                j += 1
            elif (isinstance(merged[j], str) and set(merged[j]) <= SEPARATORS
                  and j + 1 < len(merged) and isinstance(merged[j + 1], Part)):
                run.extend([merged[j], merged[j + 1]])
                j += 2
            else:
                break
        output.append(render_run(run, notes))
        i = j
    return "".join(output)


def convert_perl(code: str) -> Tuple[str, List[str]]:
    """Return (mini-language value, notes). Raises Unsupported."""
    statements = [s.strip() for s in split_top_level(code, ";") if s.strip()]
    if not statements:
        raise Unsupported("empty code")
    variables: Dict[str, Value] = {}
    result: Optional[Value] = None
    for index, statement in enumerate(statements):
        result = run_statement(statement, variables)
        if result is None and index == len(statements) - 1:
            raise Unsupported("the code does not end with a value")
    if not isinstance(result, Str):
        raise Unsupported("the code does not end with a string value")
    notes: List[str] = []
    return render(result.pieces, notes), notes


def looks_like_perl(value: str) -> bool:
    return bool(PERL_HINT.search(value))


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def load_tool():
    """Load file-transfer.py (next to this script) to validate converted values; None if missing."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file-transfer.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("file_transfer_tool_for_convert", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up here
    spec.loader.exec_module(module)
    return module


def validate(tool, key: str, value: str) -> None:
    if tool is None:
        return
    try:
        expanded = tool.expand_date_tokens(value, datetime.now())
        if key == "FileMask":
            re.compile(expanded)
        else:
            tool.build_date_filter(expanded)
    except (tool.ConfigError, re.error) as exc:
        raise Unsupported(f"the converted value '{value}' is not valid: {exc}")


@dataclass
class Change:
    key: str
    old: str
    new: str
    notes: List[str]


@dataclass
class Plan:
    path: str
    lines: List[str]
    new_lines: List[str]
    changes: List[Change] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def read_lines(path: str) -> List[str]:
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read().splitlines(keepends=True)


def plan_file(path: str, tool) -> Plan:
    lines = read_lines(path)
    plan = Plan(path, lines, list(lines))
    for index, line in enumerate(lines):
        match = LINE_RE.match(line)
        if not match or not looks_like_perl(match.group("value")):
            continue
        key, old = match.group("key"), match.group("value")
        try:
            new, notes = convert_perl(old)
            validate(tool, key, new)
        except Unsupported as exc:
            plan.problems.append(f"{key}: {exc}")
            continue
        plan.changes.append(Change(key, old, new, notes))
        plan.new_lines[index] = match.group("head") + new + match.group("tail")
    return plan


def apply_plan(plan: Plan) -> str:
    """Copy the original to <name>.param_perl, then rewrite it. Returns the backup path."""
    backup = plan.path + BACKUP_SUFFIX
    if os.path.exists(backup):
        raise FileExistsError(f"{backup} already exists, refusing to overwrite it")
    shutil.copy2(plan.path, backup)
    handle, temp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(plan.path)), prefix=".convert-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape", newline="") as out:
            out.write("".join(plan.new_lines))
        shutil.copymode(plan.path, temp_path)
        os.replace(temp_path, plan.path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return backup


def show(plan: Plan) -> None:
    print(f"\n=== {os.path.basename(plan.path)} ===")
    for change in plan.changes:
        print(f"  {change.key}")
        print(f"    - {change.old}")
        print(f"    + {change.new}")
        for note in change.notes:
            print(f"    note: {note}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Convert Perl code in .param files to date tokens")
    parser.add_argument("folder", nargs="?", default=os.getcwd(), help="folder with .param files (default: current folder)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, do not ask or write")
    parser.add_argument("--yes", action="store_true", help="convert every file without asking")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.folder):
        print(f"Folder {args.folder} does not exist", file=sys.stderr)
        return 1
    names = sorted(n for n in os.listdir(args.folder)
                   if n.endswith(".param") and os.path.isfile(os.path.join(args.folder, n)))
    tool = load_tool()
    converted = declined = failed = clean = 0
    answer_all = args.yes
    print(f"Checking {len(names)} .param file(s) in {os.path.abspath(args.folder)}")

    for name in names:
        plan = plan_file(os.path.join(args.folder, name), tool)
        if plan.problems:
            failed += 1
            print(f"\n=== {name} ===\n  CANNOT convert automatically, left unchanged:")
            for problem in plan.problems:
                print(f"    {problem}")
            continue
        if not plan.changes:
            clean += 1
            continue
        show(plan)
        if args.dry_run:
            continue
        if not answer_all:
            try:
                reply = input("Convert this file? [y]es / [n]o / [a]ll / [q]uit: ").strip().lower()
            except EOFError:
                reply = "q"
            if reply == "q":
                print("Stopped, the remaining files were not processed.")
                break
            if reply == "a":
                answer_all = True
            elif reply != "y":
                declined += 1
                print("Skipped.")
                continue
        try:
            backup = apply_plan(plan)
        except (OSError, FileExistsError) as exc:
            failed += 1
            print(f"  ERROR: {exc}", file=sys.stderr)
            continue
        converted += 1
        print(f"Converted. Original saved as {os.path.basename(backup)}")

    print(f"\nDone: {converted} converted, {declined} skipped, {failed} need manual work, {clean} needed nothing.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
