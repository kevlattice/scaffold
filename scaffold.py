#!/usr/bin/env python3
# Scaffold - CLI-to-GUI Translation Layer
# Copyright (C) 2026 Kev Wilson
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Scaffold — CLI-to-GUI Translation Layer.

Turn any command-line tool into a native desktop GUI. Scaffold reads a JSON
schema describing a CLI tool's arguments and dynamically generates an
interactive form with live command preview, process execution, presets, and
dark mode support.

Usage:
    python scaffold.py                        Launch the tool picker GUI
    python scaffold.py tools/nmap.json        Open a specific tool directly
    python scaffold.py --validate FILE        Validate a schema (no GUI)
    python scaffold.py --prompt               Print the LLM schema-generation prompt
    python scaffold.py --preset-prompt            Print the LLM preset-generation prompt
    python scaffold.py --version              Show version and exit
    python scaffold.py --help                 Show this help and exit

Requires: PySide6 (pip install PySide6) — no other dependencies.
Minimum Python version: 3.10
"""

__version__ = "2.13.1"

import atexit
import datetime
import hashlib
import html
import json
import math
import multiprocessing
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

VALID_TYPES = {"boolean", "string", "text", "integer", "float", "enum", "multi_enum", "file", "directory", "password"}
VALID_SEPARATORS = {"space", "equals", "none"}
VALID_ELEVATED = {"never", "optional", "always"}
CAPTURE_RESERVED_NAMES = frozenset({"exit_code", "stdout", "stderr", "stdout_tail", "stderr_tail", "file"})
CAPTURE_STREAM_TAIL_BYTES = 64 * 1024  # 64KB slice for regex + full-stream captures
MAX_REGEX_SECONDS = 2.0  # per-regex wall-clock budget
_REDOS_NESTED_QUANT_RE = re.compile(r"\([^)]*[+*]\)[+*]")
_REDOS_ALT_QUANT_RE = re.compile(r"\([^|)]+\|[^)]+\)[+*]")
_SHELL_METACHAR = frozenset("|;&$`(){}<>!~\x00#\\'\"")


def _user_id() -> str:
    """Return a stable user identifier for per-user temp files."""
    try:
        return str(os.getuid())
    except AttributeError:
        pass
    try:
        return os.getlogin()
    except OSError:
        return "default"


def _sanitize_filename_component(name: str) -> str:
    """Return a filename-safe version of name.

    Non-word characters (except hyphens and spaces) are replaced with '_',
    whitespace is collapsed to single underscores, and leading/trailing
    underscores are stripped. Returns '' if nothing survives — callers
    must treat that as an invalid-name signal.
    """
    safe = re.sub(r'[^\w\- ]', '_', name)
    safe = re.sub(r'\s+', '_', safe).strip('_')
    return safe


def _picker_start_dir(current: str) -> str:
    """Resolve a sensible start directory for a file/directory picker.

    If the current line-edit value points to an existing file, return its
    parent dir; if it points to an existing directory, return it as-is;
    if only the parent exists, return the parent; otherwise return "".
    """
    current = current.strip()
    if not current:
        return ""
    try:
        p = Path(current)
    except (OSError, ValueError):
        return ""
    try:
        if p.is_file():
            return str(p.parent)
        if p.is_dir():
            return str(p)
        if p.parent.exists():
            return str(p.parent)
    except OSError:
        return ""
    return ""


def _pw_copy_suffix(pw_state: str) -> str:
    """Return a status-bar suffix describing password-masking for a copy."""
    if pw_state == "real":
        return " — passwords included"
    if pw_state == "masked":
        return " — passwords masked"
    return ""


def _browse_file(line_edit: "QLineEdit", parent: "QWidget | None" = None) -> None:
    """Open a file-picker dialog; on accept, set the selected path into line_edit."""
    start_dir = _picker_start_dir(line_edit.text())
    path, _ = QFileDialog.getOpenFileName(parent, "Select File", start_dir)
    if path:
        line_edit.setText(path)


def _browse_directory(line_edit: "QLineEdit", parent: "QWidget | None" = None) -> None:
    """Open a directory-picker dialog; on accept, set the selected path into line_edit."""
    start_dir = _picker_start_dir(line_edit.text())
    path = QFileDialog.getExistingDirectory(parent, "Select Directory", start_dir)
    if path:
        line_edit.setText(path)


ARG_DEFAULTS = {
    "short_flag": None,
    "description": "",
    "required": False,
    "default": None,
    "choices": None,
    "group": None,
    "depends_on": None,
    "repeatable": False,
    "separator": "space",
    "positional": False,
    "validation": None,
    "examples": None,
    "display_group": None,
    "min": None,
    "max": None,
    "deprecated": None,
    "dangerous": False,
}

# Widget size constants
SPINBOX_RANGE = 999999
REPEAT_SPIN_MAX = 10
REPEAT_SPIN_WIDTH = 75
TEXT_WIDGET_HEIGHT = 80
MULTI_ENUM_HEIGHT = 120

# Default window dimensions
DEFAULT_WINDOW_WIDTH = 700
DEFAULT_WINDOW_HEIGHT = 750
MIN_WINDOW_WIDTH = 530
MIN_WINDOW_HEIGHT = 400

# Output panel limits
OUTPUT_MAX_BLOCKS = 10000       # Max lines kept in the output panel
OUTPUT_FLUSH_MS = 100           # Milliseconds between output buffer flushes
OUTPUT_BUFFER_MAX_BYTES = 50 * 1024 * 1024  # 50 MB soft cap on _output_buffer
OUTPUT_MIN_HEIGHT = 80          # Minimum output panel height (pixels)
OUTPUT_DEFAULT_HEIGHT = 150     # Default output panel height (pixels)

# Tool schema file size limit (bytes)
MAX_SCHEMA_SIZE = 1_000_000     # 1 MB — skip files larger than this

# Auto-save / crash recovery
AUTOSAVE_DEBOUNCE_MS = 2000     # Debounce delay — save 2s after last field change
AUTOSAVE_EXPIRY_HOURS = 24      # Recovery files older than this are ignored

# Command history
HISTORY_MAX_ENTRIES = 50

# Per-entry size cap for history. Prevents Windows registry key-size
# limits from silently discarding writes when a user runs commands with
# extremely large extra_flags or text-field values. An oversized entry
# is stored with preset_data replaced by a sentinel string so the user
# can still see the display/timestamp/exit_code but can't restore the
# form from the entry.
HISTORY_ENTRY_MAX_BYTES = 64 * 1024  # 64 KB per entry

# Aggregate cap for the full history JSON blob. Belt-and-suspenders
# guard against 50 entries at 63.9 KB each blowing past registry limits.
HISTORY_TOTAL_MAX_BYTES = 1 * 1024 * 1024  # 1 MB total

# Cascade history
CASCADE_HISTORY_MAX_ENTRIES = 50
# Bumped only when the cascade_history entry schema changes in a way
# that isn't backward-readable. If you bump this, add an explicit
# migration in _load_cascade_history — the current code silently drops
# all history on version mismatch.
CASCADE_HISTORY_VERSION = 1


def _enforce_history_entry_size(entry: dict) -> dict:
    """Return a copy of entry with preset_data dropped if the serialized
    entry exceeds HISTORY_ENTRY_MAX_BYTES. The display/timestamp/exit_code
    fields are always preserved so the user still sees the run happened."""
    try:
        size = len(json.dumps(entry).encode("utf-8"))
    except (TypeError, ValueError):
        size = HISTORY_ENTRY_MAX_BYTES + 1  # treat un-serializable as oversized
    if size <= HISTORY_ENTRY_MAX_BYTES:
        return entry
    shrunk = dict(entry)
    shrunk["preset_data"] = None
    shrunk["_oversized"] = True
    return shrunk


def _enforce_history_total_size(entries: list[dict]) -> list[dict]:
    """Trim the history list from the oldest end until the serialized
    total fits under HISTORY_TOTAL_MAX_BYTES. Always keeps at least the
    most recent entry, even if it individually exceeds the cap."""
    if not entries:
        return entries
    while len(entries) > 1:
        try:
            size = len(json.dumps(entries).encode("utf-8"))
        except (TypeError, ValueError):
            return [entries[0]]  # pathological — keep only newest
        if size <= HISTORY_TOTAL_MAX_BYTES:
            return entries
        entries = entries[:-1]
    return entries


# ---------------------------------------------------------------------------
# JSON loader, validator, normalizer
# ---------------------------------------------------------------------------

def load_tool(path: str | Path) -> dict:
    """Read and parse a JSON tool file. Raises RuntimeError on failure."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"File not found: {p}")
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    if size > MAX_SCHEMA_SIZE:
        raise RuntimeError(
            f"Schema file too large ({size:,} bytes, limit {MAX_SCHEMA_SIZE:,}): {p.name}"
        )
    try:
        text = p.read_text(encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"Permission denied: {p}")
    except OSError as e:
        raise RuntimeError(f"Cannot read file: {p} — {e}")
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {p} — {e}")


def _read_json_file(path) -> dict:
    """Read a JSON file, stripping UTF-8 BOM if present.

    Accepts str or Path. Raises OSError on read failure and
    json.JSONDecodeError on parse failure. Callers handle errors.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text)


MAX_PRESET_SIZE = 2 * 1024 * 1024  # 2 MB cap for user JSON files


def _read_user_json(path, max_size: int = MAX_PRESET_SIZE) -> dict:
    """Read a user-supplied JSON file with size cap.

    Wraps _read_json_file with a pre-read size check. Use for
    any JSON that originates outside Scaffold (imported
    presets/cascades, preset files referenced by cascade steps,
    user-picked files from dialogs). Do NOT use for Scaffold's
    own writes (recovery files, atomic-written QSettings-backed
    data) — those are size-bounded by our own serialization.

    Raises RuntimeError on oversize; propagates OSError and
    JSONDecodeError for callers that already handle them.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as e:
        raise RuntimeError(f"Cannot stat file: {p} — {e}")
    if size > max_size:
        raise RuntimeError(
            f"File too large ({size:,} bytes, limit {max_size:,}): {p.name}"
        )
    return _read_json_file(p)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: write to temp, then os.replace.

    Raises OSError on failure; callers handle it. The temp file is cleaned
    up on failure so partial writes don't leave debris.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def validate_tool(data: dict) -> list[str]:
    """Validate a tool dict against the schema. Returns a list of error strings."""
    errors = []

    if not isinstance(data, dict):
        return [f"Tool schema must be a dict, got {type(data).__name__}"]

    for key in ("tool", "binary", "description", "arguments"):
        if key not in data:
            errors.append(f"Missing required top-level key: \"{key}\"")

    # Validate "tool" field shape — security-sensitive (used as a directory
    # name under presets/ and default_presets/; must not enable path traversal).
    # Allows letters, digits, spaces, and "_.()-" so shipped human-friendly
    # names like "Ansible (Ad-Hoc)" and "Docker Compose" remain valid.
    if "tool" in data:
        tool_name = data["tool"]
        if not isinstance(tool_name, str) or not tool_name:
            errors.append("\"tool\" must be a non-empty string")
        elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.() -]{0,63}$", tool_name):
            errors.append(
                "Invalid \"tool\": must be 1-64 chars of letters/digits/spaces and "
                "\"_.()-\", starting with alphanumeric"
            )
        elif ".." in tool_name:
            errors.append("Invalid \"tool\": must not contain \"..\"")
        elif tool_name != tool_name.strip():
            errors.append("Invalid \"tool\": must not have leading or trailing whitespace")

    # Validate binary field — security-sensitive (passed to QProcess.setProgram)
    if "binary" in data:
        binary = data["binary"]
        if not isinstance(binary, str) or not binary.strip():
            errors.append("\"binary\" must be a non-empty string")
        elif len(binary) > 256:
            errors.append(f"\"binary\" too long ({len(binary)} chars, limit 256)")
        else:
            # Check for null bytes first (dedicated message)
            if "\x00" in binary:
                errors.append("\"binary\" contains null bytes")
            # Reject leading-dash binary names. On Windows, get_elevation_command
            # does not insert a "--" options-terminator before the binary, so a
            # binary like "--copyns" or "-i" would be swallowed by gsudo as its
            # own option. Rejecting here is defense-in-depth on all platforms.
            if binary and binary[0] == "-":
                errors.append(
                    "\"binary\" cannot start with \"-\" "
                    "(would be interpreted as an option flag by elevation helpers)"
                )
            # Check for shell metacharacters (never valid in a binary name)
            # '\\' is excluded here because Windows absolute paths (e.g.
            # C:\\Windows\\System32\\ping.exe) use it as a separator. The subcommand-
            # name check below does NOT carve it out — subcommand names are tokens,
            # not paths.
            bad_chars = sorted(set(binary) & (_SHELL_METACHAR - {"\x00", "\\"}))
            if bad_chars:
                errors.append(
                    f"\"binary\" contains shell metacharacters: {' '.join(bad_chars)}"
                )
            # Check for whitespace in binary name
            if any(c.isspace() for c in binary):
                errors.append("\"binary\" contains whitespace")
            # Check for path separators — only allowed if the path is absolute
            has_sep = "/" in binary or "\\" in binary
            if has_sep:
                is_absolute = (
                    binary.startswith("/")  # Unix absolute
                    or (len(binary) >= 3 and binary[1] == ":" and binary[2] in "/\\")  # Windows C:\
                )
                if not is_absolute:
                    errors.append(
                        "\"binary\" contains path separators but is not an absolute path "
                        "(use a bare executable name or an absolute path)"
                    )

    if "arguments" in data and not isinstance(data["arguments"], list):
        errors.append("\"arguments\" must be a list")

    if "arguments" in data and isinstance(data["arguments"], list):
        _validate_args(data["arguments"], "top-level", errors)
        _check_duplicate_flag_values(data["arguments"], "global scope", errors)
        _check_groups(data["arguments"], "top-level", errors)
        _check_dependencies(data["arguments"], "top-level", errors)

    if data.get("elevated") is not None:
        if data["elevated"] not in VALID_ELEVATED:
            errors.append(
                f"Invalid \"elevated\" value \"{data['elevated']}\" "
                f"(must be one of: {', '.join(sorted(VALID_ELEVATED))}, or null)"
            )

    if data.get("subcommands") is not None:
        if not isinstance(data["subcommands"], list):
            errors.append("\"subcommands\" must be a list or null")
        else:
            seen_sub_names = {}
            for i, sub in enumerate(data["subcommands"]):
                label = f"subcommand[{i}]"
                if not isinstance(sub, dict):
                    errors.append(f"{label}: must be an object")
                    continue
                if "name" not in sub:
                    errors.append(f"{label}: missing required key \"name\"")
                else:
                    name = sub["name"]
                    if not isinstance(name, str) or not name.strip():
                        errors.append(f"{label}: \"name\" must be a non-empty string")
                    elif name != name.strip():
                        errors.append(f"subcommand \"{name}\": \"name\" has leading/trailing whitespace")
                    elif "  " in name:
                        errors.append(f"subcommand \"{name}\": \"name\" contains double spaces")
                    elif any(c in name for c in "\n\r\t"):
                        errors.append(f"subcommand name {name!r}: contains illegal whitespace characters")
                    elif name == "__global__":
                        # Reserved sentinel used by ToolForm to scope global-arg
                        # field keys; a subcommand with this name would collide
                        # with global-arg field registration. Hardcoded here
                        # rather than referencing ToolForm.GLOBAL because
                        # validate_tool runs at schema-load time, before any
                        # ToolForm exists.
                        errors.append("Invalid subcommand name \"__global__\": reserved identifier")
                    else:
                        _bad_token = None
                        for _tok in name.split():
                            if _tok.startswith("-"):
                                _bad_token = _tok
                                break
                            if any(c in _SHELL_METACHAR for c in _tok) or any(c in _tok for c in "\n\r\t"):
                                _bad_token = _tok
                                break
                        if _bad_token is not None:
                            errors.append(
                                f"subcommand name {name!r}: token {_bad_token!r} "
                                f"cannot start with '-' or contain shell metacharacters"
                            )
                        elif name in seen_sub_names:
                            errors.append(f"duplicate subcommand name \"{name}\"")
                        seen_sub_names[name] = True
                    label = f"subcommand \"{name}\""
                if "arguments" not in sub:
                    errors.append(f"{label}: missing required key \"arguments\"")
                elif not isinstance(sub["arguments"], list):
                    errors.append(f"{label}: \"arguments\" must be a list")
                else:
                    _validate_args(sub["arguments"], label, errors)
                    sub_name = sub.get("name", "?")
                    _check_duplicate_flag_values(sub["arguments"], f"subcommand '{sub_name}'", errors)
                    _check_groups(sub["arguments"], label, errors)
                    _check_dependencies(sub["arguments"], label, errors)

    return errors


def _validate_args(args: list, scope: str, errors: list) -> None:
    for i, arg in enumerate(args):
        if not isinstance(arg, dict):
            errors.append(f"{scope} argument[{i}]: must be an object")
            continue
        name = arg.get("name", f"argument[{i}]")
        prefix = f"{scope} \"{name}\""

        for key in ("name", "flag", "type"):
            if key not in arg:
                errors.append(f"{prefix}: missing required key \"{key}\"")

        if "flag" in arg:
            if not isinstance(arg["flag"], str):
                errors.append(f"{prefix}: \"flag\" must be a string, got {type(arg['flag']).__name__}")
            else:
                if arg["flag"] != arg["flag"].strip():
                    errors.append(f"{prefix}: flag \"{arg['flag']}\" has leading/trailing whitespace")
                if arg["flag"].startswith("_"):
                    errors.append(f"{prefix}: flag \"{arg['flag']}\" starts with \"_\" which is reserved for internal metadata")

        if "type" in arg:
            t = arg["type"]
            if t not in VALID_TYPES:
                errors.append(f"{prefix}: invalid type \"{t}\" (must be one of: {', '.join(sorted(VALID_TYPES))})")
            if t in ("enum", "multi_enum"):
                choices = arg.get("choices")
                if not isinstance(choices, list) or len(choices) == 0:
                    errors.append(f"{prefix}: type \"{t}\" requires a non-empty \"choices\" list")
                # Required enum/multi_enum must declare an explicit default —
                # otherwise the widget silently selects the first choice and
                # the user never sees a "no value chosen" state. Empty list []
                # is a valid multi_enum default; only None / missing is rejected.
                if arg.get("required") is True:
                    if "default" not in arg or arg.get("default") is None:
                        flag_label = arg.get("flag", arg.get("name", "?"))
                        errors.append(
                            f"Argument \"{flag_label}\": required {t} must specify a non-null \"default\""
                        )

        if "separator" in arg and arg["separator"] not in VALID_SEPARATORS:
            errors.append(f"{prefix}: invalid separator \"{arg['separator']}\" (must be one of: {', '.join(sorted(VALID_SEPARATORS))})")

        if "examples" in arg and arg["examples"] is not None:
            if not isinstance(arg["examples"], list) or not all(isinstance(e, str) for e in arg["examples"]):
                errors.append(f"{prefix}: \"examples\" must be a list of strings or null")
            if "type" in arg and arg["type"] == "enum":
                errors.append(f"{prefix}: has both \"choices\" and \"examples\" set. For enum types, \"choices\" is used and \"examples\" is ignored")
            if "type" in arg and arg["type"] == "password":
                errors.append(f"{prefix}: \"examples\" should not be used with password type (sensitive values should not be suggested)")

        if "display_group" in arg and arg["display_group"] is not None:
            if not isinstance(arg["display_group"], str):
                errors.append(f"{prefix}: \"display_group\" must be a string or null")

        # min/max validation
        for bound in ("min", "max"):
            if bound in arg and arg[bound] is not None:
                if not isinstance(arg[bound], (int, float)):
                    errors.append(f"{prefix}: \"{bound}\" must be a number or null")
                elif "type" in arg and arg["type"] not in ("integer", "float"):
                    errors.append(f"{prefix}: \"{bound}\" is only valid for integer and float types")
        if (arg.get("min") is not None and arg.get("max") is not None
                and isinstance(arg.get("min"), (int, float))
                and isinstance(arg.get("max"), (int, float))):
            if arg["min"] > arg["max"]:
                errors.append(f"{prefix}: \"min\" ({arg['min']}) must be <= \"max\" ({arg['max']})")

        # Reject integer min/max at Qt QSpinBox's 32-bit C int boundaries
        # where min - 1 (sentinel) or max + 1 (headroom) would overflow and
        # silently fall back to QLineEdit. Float bounds are unaffected —
        # QDoubleSpinBox handles the full Python float range.
        if arg.get("type") == "integer":
            _imin = arg.get("min")
            if isinstance(_imin, (int, float)) and _imin <= -2147483647:
                errors.append(
                    f"{prefix}: integer \"min\" ({_imin}) is below the safe threshold "
                    f"(-2147483647); Qt integer widgets cannot represent min - 1 for the unset sentinel"
                )
            _imax = arg.get("max")
            if isinstance(_imax, (int, float)) and _imax >= 2147483646:
                errors.append(
                    f"{prefix}: integer \"max\" ({_imax}) is above the safe threshold "
                    f"(2147483646); Qt integer widgets cannot represent max + 1 for range headroom"
                )

        # deprecated validation
        if "deprecated" in arg and arg["deprecated"] is not None:
            if not isinstance(arg["deprecated"], str):
                errors.append(f"{prefix}: \"deprecated\" must be a string or null")
            elif arg["deprecated"] == "":
                errors.append(f"{prefix}: \"deprecated\" must be a non-empty string or null")

        # dangerous validation
        if "dangerous" in arg and not isinstance(arg["dangerous"], bool):
            errors.append(f"{prefix}: \"dangerous\" must be a boolean")

        # validation regex (F2): reject ReDoS-prone patterns at schema-load
        # time so the downstream re.compile(arg["validation"]) sites in
        # ToolForm widgets cannot be fed a catastrophic-backtracking regex.
        v = arg.get("validation")
        if isinstance(v, str) and v:
            is_prone, reason = _pattern_is_redos_prone(v)
            if is_prone:
                errors.append(f"{prefix}: \"validation\" regex has {reason}")


def _check_duplicate_flag_values(args: list, scope_label: str, errors: list) -> None:
    """Check for duplicate flag and short_flag values within a single scope.

    Covers both flag-style (starting with '-') and positional-style arguments.
    """
    seen_flags: dict[str, str] = {}
    seen_shorts: dict[str, str] = {}
    for arg in args:
        if not isinstance(arg, dict):
            continue
        name = arg.get("name", "?")
        flag = arg.get("flag")
        if isinstance(flag, str):
            flag = flag.strip()
        if isinstance(flag, str) and flag:
            if flag in seen_flags:
                errors.append(
                    f"Duplicate flag '{flag}' in {scope_label}: "
                    f"used by '{seen_flags[flag]}' and '{name}'"
                )
            else:
                seen_flags[flag] = name
        short = arg.get("short_flag")
        if isinstance(short, str):
            short = short.strip()
        if isinstance(short, str) and short.startswith("-"):
            if short in seen_shorts:
                errors.append(
                    f"Duplicate short_flag '{short}' in {scope_label}: "
                    f"used by '{seen_shorts[short]}' and '{name}'"
                )
            else:
                seen_shorts[short] = name


def _check_groups(args: list, scope: str, errors: list) -> None:
    groups = {}
    for arg in args:
        if not isinstance(arg, dict):
            continue
        g = arg.get("group")
        if g:
            groups.setdefault(g, []).append(arg.get("name", "?"))
    for g, members in groups.items():
        if len(members) < 2:
            errors.append(f"{scope}: group \"{g}\" has only 1 member (\"{members[0]}\") — mutual exclusivity requires at least 2")


def _check_dependencies(args: list, scope: str, errors: list) -> None:
    """Validate that every depends_on reference points to an existing flag in the same scope."""
    flags = set()
    for arg in args:
        if not isinstance(arg, dict):
            continue
        flag = arg.get("flag")
        if flag:
            flags.add(flag)
    for arg in args:
        if not isinstance(arg, dict):
            continue
        dep = arg.get("depends_on")
        if dep and dep not in flags:
            name = arg.get("name", "?")
            errors.append(f"{scope}: argument \"{name}\" depends on \"{dep}\" which does not exist in this scope")


def normalize_tool(data: dict) -> dict:
    """Fill in missing optional fields with safe defaults.

    Returns a deep copy with defaults filled in; the original is never mutated.
    This function is defensive: callers may pass unvalidated data and we avoid
    raising AttributeError/TypeError by skipping non-list/non-dict shapes.
    Raises TypeError if data itself is not a dict — the contract is dict->dict
    and a non-dict at this level is a programmer error, not a data issue.
    """
    if not isinstance(data, dict):
        raise TypeError(f"normalize_tool expected dict, got {type(data).__name__}")

    data = deepcopy(data)
    data.setdefault("subcommands", None)
    data.setdefault("description", "")
    data.setdefault("elevated", None)

    def _normalize_args(args):
        if not isinstance(args, list):
            return
        for arg in args:
            if not isinstance(arg, dict):
                continue
            for key, default in ARG_DEFAULTS.items():
                arg.setdefault(key, default)
            if isinstance(arg.get("flag"), str):
                arg["flag"] = arg["flag"].strip()

    _normalize_args(data.get("arguments", []))

    if data.get("subcommands"):
        if isinstance(data["subcommands"], list):
            for sub in data["subcommands"]:
                if not isinstance(sub, dict):
                    continue
                sub.setdefault("description", "")
                _normalize_args(sub.get("arguments", []))

    return data


def schema_hash(data: dict) -> str:
    """Compute a short hash of a tool's argument flags for preset versioning."""
    flags = sorted(
        arg.get("flag") for arg in data.get("arguments", [])
        if isinstance(arg, dict) and isinstance(arg.get("flag"), str)
    )
    if data.get("subcommands"):
        for sub in data["subcommands"]:
            if not isinstance(sub, dict) or not isinstance(sub.get("name"), str):
                continue
            for arg in sub.get("arguments", []):
                if isinstance(arg, dict) and isinstance(arg.get("flag"), str):
                    flags.append(f"{sub.get('name')}:{arg.get('flag')}")
        flags.sort()
    return hashlib.sha256(json.dumps(flags).encode()).hexdigest()[:8]


# ANSI escape sequence pattern — compiled once, used in _flush_output()
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

# Maximum length for any single string key or value in a preset
_PRESET_MAX_STRING_LEN = 10_000

# Safe value types allowed in presets
_PRESET_SAFE_TYPES = (str, int, float, bool, list, type(None))

# Preset meta-key type contract. Validators reject wrong types before
# apply_values is called. Recovery file meta-keys (_recovery_*) are NOT
# covered here — they are checked inline in _check_for_recovery /
# _cleanup_stale_recovery_files. Cascade-file meta-keys (_version, and
# _format with non-"scaffold_preset" values) are a separate file format
# and are validated at their own load sites.
PRESET_META_KEY_TYPES: dict[str, tuple[type, ...]] = {
    "_format": (str,),
    "_tool": (str,),
    "_subcommand": (str, type(None)),
    "_schema_hash": (str,),
    "_elevated": (bool,),
    "_extra_flags": (str,),
    "_description": (str,),
}

# Per-field type enforcement for preset values. Maps each scaffold field
# type to the tuple of acceptable Python types for its preset value.
# `bool` is excluded from `int`/`float` despite being a subclass because
# Python's bool/int conflation produces silently-wrong form state on
# preset load. None is always accepted (= "field unset"). The repeatable-
# boolean exception (int repeat count) is handled inline in
# _check_preset_value_type, not here.
PRESET_FIELD_TYPE_RULES: dict[str, tuple[type, ...]] = {
    "boolean":    (bool,),
    "string":     (str,),
    "text":       (str,),
    "integer":    (int,),
    "float":      (int, float),
    "enum":       (str,),
    "multi_enum": (list,),
    "file":       (str,),
    "directory":  (str,),
    "password":   (str,),
}


def _check_preset_value_type(arg: dict, value) -> str | None:
    """Validate a preset value against its declared scaffold field type.

    Returns an error message string if the type is wrong, or None if the
    value is acceptable. None values always pass (= "field unset").

    Takes the full arg dict (not just the type string) so it can honour
    the repeatable-boolean storage convention: serialize_values returns
    the QSpinBox repeat count (int) for repeatable booleans, so int is
    valid there even though the field type is "boolean".
    """
    if value is None:
        return None
    field_type = arg.get("type")
    expected = PRESET_FIELD_TYPE_RULES.get(field_type)
    if expected is None:
        # Unknown field type — defer to general structural validation.
        return None
    # Repeatable booleans serialize as int (the repeat count from the
    # adjacent QSpinBox); bool is also accepted for unrepeated values.
    if field_type == "boolean" and arg.get("repeatable"):
        if not isinstance(value, (bool, int)):
            return f"expected bool or int (repeat count), got {type(value).__name__}"
        return None
    # Reject bool for integer/float fields. Python's isinstance(True, int)
    # is True, but treating bool as numeric here would let `{"--count": True}`
    # pass for an integer field, which is silently wrong.
    if field_type in ("integer", "float") and isinstance(value, bool):
        return f"expected {field_type}, got bool"
    if not isinstance(value, expected):
        expected_names = " or ".join(t.__name__ for t in expected)
        return f"expected {expected_names}, got {type(value).__name__}"
    return None


def validate_preset(data, tool_data=None, report_unknown_keys=True) -> list[str]:
    """Validate a preset dict. Returns a list of error strings (empty = valid).

    Checks structure, value types, and string lengths of *data*.

    If *tool_data* is provided, additionally checks unknown keys (keys that
    don't match any flag defined in the tool's schema) and per-key value
    types against the schema's declared field types. v2.11.4 wired this
    into production load callers (cascade slot click, chain advance,
    UI preset load); historically it was only used by the cohort guard in
    test_preset_validation.py §30.

    *report_unknown_keys* gates the unknown-key check. Production load
    callers pass False so the schema_hash mismatch path remains the
    canonical "stale preset" gate (it produces a more useful diagnostic
    than per-flag unknown-key spam). Tests and the cohort guard pass the
    default True. Per-key type errors fire regardless.
    """
    errors = []

    if not isinstance(data, dict):
        errors.append(f"Preset must be a dict, got {type(data).__name__}")
        return errors

    # Check all keys are strings
    for key in data:
        if not isinstance(key, str):
            errors.append(f"Preset key must be a string, got {type(key).__name__}: {key!r}")
            continue

        if len(key) > _PRESET_MAX_STRING_LEN:
            errors.append(f"Preset key too long ({len(key)} chars, limit {_PRESET_MAX_STRING_LEN})")

    # Detect schema-as-preset mistake
    if "binary" in data and "arguments" in data:
        errors.append("This looks like a tool schema, not a preset")

    # Check values
    for key, value in data.items():
        if not isinstance(key, str):
            continue  # already reported above

        # Meta keys — keys starting with _ and not containing ":" (field keys
        # use scope:flag format like "__global__:--verbose"). Registered keys
        # are type-checked against PRESET_META_KEY_TYPES; None is treated as
        # missing; unknown _foo keys pass for forward-compat with future
        # scaffold versions that may add new meta-keys.
        if key.startswith("_") and ":" not in key:
            # None-as-missing — skip silently for both registered and unknown keys.
            if value is None:
                continue
            expected = PRESET_META_KEY_TYPES.get(key)
            if expected is not None:
                if not isinstance(value, expected):
                    expected_names = " or ".join(t.__name__ for t in expected)
                    errors.append(
                        f"Preset meta-key \"{key}\" has wrong type {type(value).__name__} "
                        f"(expected {expected_names})"
                    )
                    continue
            else:
                # Unknown meta-key — pass (forward-compat) but log for debugging.
                print(f"[preset] unknown meta-key: {key!r}", file=sys.stderr)
            # Preserve string-length check on string meta values.
            if isinstance(value, str) and len(value) > _PRESET_MAX_STRING_LEN:
                errors.append(
                    f"Preset value for \"{key}\" too long ({len(value)} chars, limit {_PRESET_MAX_STRING_LEN})"
                )
            continue

        if not isinstance(value, _PRESET_SAFE_TYPES):
            errors.append(
                f"Preset value for \"{key}\" has unsupported type {type(value).__name__} "
                f"(allowed: str, int, float, bool, list, None)"
            )
            continue

        # List values must contain only strings (multi_enum storage)
        if isinstance(value, list):
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    errors.append(
                        f"Preset list value for \"{key}\"[{i}] must be a string, "
                        f"got {type(item).__name__}"
                    )

        # String length check on values
        if isinstance(value, str) and len(value) > _PRESET_MAX_STRING_LEN:
            errors.append(
                f"Preset value for \"{key}\" too long ({len(value)} chars, limit {_PRESET_MAX_STRING_LEN})"
            )

    # Optional: schema-aware checks if tool_data is provided. Two checks run
    # here: unknown-key warnings (preset references a flag not in the schema)
    # and per-key type enforcement (preset value's Python type doesn't match
    # the schema's declared field type). Both run only when the structural
    # checks above produced no errors, keeping diagnostics ordered.
    if tool_data is not None and not errors:
        known_args: dict[str, dict] = {}
        for arg in tool_data.get("arguments", []):
            if isinstance(arg, dict) and "flag" in arg and "type" in arg:
                known_args[arg["flag"]] = arg
        for sub in tool_data.get("subcommands", None) or []:
            for arg in sub.get("arguments", []):
                if isinstance(arg, dict) and "flag" in arg and "type" in arg:
                    known_args[f"{sub['name']}:{arg['flag']}"] = arg

        for key in data:
            if key.startswith("_") and ":" not in key:
                continue
            if key not in known_args:
                if report_unknown_keys:
                    errors.append(f"Unknown preset key \"{key}\" — not found in current schema (may be from an older version)")
                continue
            msg = _check_preset_value_type(known_args[key], data[key])
            if msg is not None:
                errors.append(f"Preset value for \"{key}\" has wrong type: {msg}")

    return errors


# ---------------------------------------------------------------------------
# Elevation helpers
# ---------------------------------------------------------------------------

_elevation_tool = None  # cached result: str path or None
_already_elevated = None  # cached result: bool


def _check_already_elevated():
    """Check if the app is already running with elevated privileges."""
    global _already_elevated
    if _already_elevated is not None:
        return _already_elevated
    if sys.platform == "win32":
        try:
            import ctypes
            _already_elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, RuntimeError):
            _already_elevated = False
    else:
        import os
        _already_elevated = os.geteuid() == 0
    return _already_elevated


def _find_elevation_tool():
    """Find the platform-appropriate elevation tool. Cached after first call."""
    global _elevation_tool
    if _elevation_tool is not None:
        return _elevation_tool
    if sys.platform == "win32":
        _elevation_tool = shutil.which("gsudo") or ""
    else:
        _elevation_tool = shutil.which("pkexec") or ""
    return _elevation_tool


def get_elevation_command(cmd_list: list[str]) -> tuple[list[str], str | None]:
    """
    Returns (elevated_cmd_list, error_message).
    If elevation is available, returns the modified command and None.
    If not available, returns the original command and an error message.
    """
    tool = _find_elevation_tool()
    if tool:
        # pkexec(1) documents `--` as the options terminator between itself
        # and the target command. Binary-name validation does not reject a
        # leading `-`, so a hostile schema with e.g. "binary": "--user" could
        # otherwise be parsed as a pkexec option. gsudo (win32) is excluded —
        # older gsudo versions reject `--` and the convention does not apply.
        return ([tool] if sys.platform == "win32" else [tool, "--"]) + cmd_list, None

    if sys.platform == "win32":
        return cmd_list, (
            "Elevated execution requires gsudo.\n"
            "Install it with: winget install gerardog.gsudo\n"
            "Or relaunch Scaffold as Administrator (right-click > Run as administrator)."
        )
    elif sys.platform == "darwin":
        return cmd_list, (
            "Elevated execution requires pkexec.\n"
            "Install it with: brew install polkit\n"
            "Or run Scaffold itself with sudo."
        )
    else:
        return cmd_list, (
            "Elevated execution requires PolicyKit (pkexec).\n"
            "Install it with your package manager, or run Scaffold itself with sudo."
        )


def _elevation_label():
    """Return platform-appropriate label for the elevation checkbox."""
    if sys.platform == "win32":
        return "Run as Administrator"
    return "Run with elevated privileges (sudo)"


# ---------------------------------------------------------------------------
# GUI renderer
# ---------------------------------------------------------------------------

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QProcess, QProcessEnvironment, QSettings, QStandardPaths, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPalette, QPen, QPolygon, QShortcut, QTextCharFormat, QTextCursor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QInputDialog, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QSizePolicy, QSpacerItem, QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


_dark_mode = False
_original_palette = None
_arrow_dir = None
_arrow_cleanup_registered = False


def _cleanup_arrow_dir():
    """Remove the temp arrow icon directory. Best-effort, silent."""
    if _arrow_dir is not None:
        shutil.rmtree(_arrow_dir, ignore_errors=True)


def _make_arrow_icons():
    """Generate small arrow PNGs for dark mode dropdown/spinbox controls."""
    global _arrow_dir, _arrow_cleanup_registered
    if _arrow_dir is not None:
        return _arrow_dir
    app = QApplication.instance()
    if app is None:
        return None
    _arrow_dir = tempfile.mkdtemp(prefix="scaffold_arrows_")
    if not _arrow_cleanup_registered:
        atexit.register(_cleanup_arrow_dir)
        _arrow_cleanup_registered = True
    color = QColor(DARK_COLORS["text"])
    for name, points in [
        ("down", [(1, 2), (5, 6), (9, 2)]),
        ("up", [(1, 6), (5, 2), (9, 6)]),
    ]:
        img = QImage(10, 8, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygon([QPoint(*p) for p in points]))
        painter.end()
        img.save(f"{_arrow_dir}/{name}.png")
    return _arrow_dir


DARK_COLORS = {
    "window": "#1e1e2e",
    "widget": "#2a2a3c",
    "input": "#313244",
    "text": "#cdd6f4",
    "text_dim": "#a6adc8",
    "accent": "#89b4fa",
    "selection": "#45475a",
    "border": "#585b70",
    "required": "#f38ba8",
    "disabled": "#6c7086",
    "success": "#a6e3a1",
    "error": "#f38ba8",
    "stderr": "#fab387",
    "output_bg": "#11111b",
    "output_text": "#cdd6f4",
    "command": "#89b4fa",
    "warning_bg": "#45475a",
    "warning_text": "#fab387",
    "warning_border": "#585b70",
}

# Preview syntax coloring — light mode
LIGHT_PREVIEW = {
    "binary": "#0550ae",       # blue, bold
    "flag": "#953800",         # amber/orange
    "value": "#24292f",        # default text
    "subcommand": "#0550ae",   # blue, bold
    "extra": "#656d76",        # dimmed gray
}

# Preview syntax coloring — dark mode
DARK_PREVIEW = {
    "binary": "#89b4fa",       # light blue, bold
    "flag": "#fab387",         # peach/orange
    "value": "#cdd6f4",        # default text
    "subcommand": "#89b4fa",   # light blue, bold
    "extra": "#a6adc8",        # dimmed
}


# Status/output colors — theme-independent (output panel is always dark)
COLOR_OK = "#4ec94e"       # success green
COLOR_ERR = "#e05555"      # error red
COLOR_WARN = "#e8a838"     # warning amber (stderr, stopped, cancelled)
COLOR_CMD = "#569cd6"      # command echo blue
COLOR_DIM = "#888888"      # dimmed text (unavailable tools in picker)
OUTPUT_BG = "#1e1e1e"      # output panel background
OUTPUT_FG = "#d4d4d4"      # output panel default foreground

# Light-mode warning bar colors
LIGHT_WARNING_BG = "#fff3cd"
LIGHT_WARNING_FG = "#856404"
LIGHT_WARNING_BORDER = "#ffc107"


def _detect_system_dark() -> bool:
    """Return True if the OS is currently using a dark color scheme."""
    try:
        scheme = QApplication.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Dark
    except AttributeError:
        pass
    try:
        lightness = QApplication.palette().color(QPalette.ColorRole.Window).lightness()
        return lightness < 128
    except (AttributeError, RuntimeError, TypeError):
        pass
    return False


def apply_theme(dark: bool) -> None:
    """Apply or remove the dark palette and stylesheet application-wide."""
    global _dark_mode, _original_palette
    _dark_mode = dark
    app = QApplication.instance()
    if _original_palette is None:
        _original_palette = QPalette(app.palette())
    if dark:
        # Tell the platform to use its native dark mode for controls that
        # are not overridden by QSS (notably scrollbars).  This preserves
        # all native behavior — smooth animations, hover expansion, arrow
        # buttons — with dark colors, because the same native renderer
        # draws them.  Requires Qt 6.8+ (PySide6 6.8+); gracefully ignored
        # on older versions where scrollbars will fall back to palette hints.
        try:
            app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
        except AttributeError:
            pass  # Qt < 6.8 — native dark scrollbars not available
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(DARK_COLORS["window"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(DARK_COLORS["input"]))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(DARK_COLORS["widget"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(DARK_COLORS["widget"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(DARK_COLORS["accent"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(DARK_COLORS["window"]))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(DARK_COLORS["widget"]))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(DARK_COLORS["disabled"]))
        palette.setColor(QPalette.ColorRole.Link, QColor(DARK_COLORS["accent"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(DARK_COLORS["disabled"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(DARK_COLORS["disabled"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(DARK_COLORS["disabled"]))
        app.setPalette(palette)
        arrow_dir = (_make_arrow_icons() or "").replace("\\", "/")
        C = {**DARK_COLORS, "arrow_dir": arrow_dir}
        app.setStyleSheet(
            # Menu bar and menus
            f"QMenuBar {{ background-color: {C['widget']}; color: {C['text']}; }}"
            f"QMenuBar::item {{ background: transparent; padding: 4px 10px; }}"
            f"QMenuBar::item:selected {{ background-color: {C['selection']}; }}"
            f"QMenu {{ background-color: {C['widget']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px; }}"
            f"QMenu::item:selected {{ background-color: {C['selection']}; }}"
            # Checkboxes — padding restores Fusion's native sizeHint height
            f"QCheckBox {{ color: {C['text']}; padding: 4px 0; }}"
            f"QCheckBox::indicator {{ background-color: {C['selection']};"
            f"  border: 1px solid {C['border']}; border-radius: 2px; }}"
            f"QCheckBox::indicator:checked {{ background-color: {C['accent']};"
            f"  border: 1px solid {C['accent']}; border-radius: 2px; }}"
            # Comboboxes (dropdowns) — padding: 3px 4px restores 26px height
            f"QComboBox {{ background-color: {C['input']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px;"
            f"  padding: 3px 4px; }}"
            f"QComboBox QAbstractItemView {{ background-color: {C['widget']};"
            f"  color: {C['text']}; selection-background-color: {C['selection']};"
            f"  selection-color: {C['text']}; border: 1px solid {C['border']};"
            f"  border-radius: 3px; }}"
            f"QComboBox::drop-down {{ border-left: 1px solid {C['border']};"
            f"  background-color: {C['selection']}; }}"
            f"QComboBox::down-arrow {{ image: url({C['arrow_dir']}/down.png);"
            f"  width: 10px; height: 8px; }}"
            # Spinboxes — padding: 0px 2px yields ~24px, within 2px of native 22px
            f"QSpinBox, QDoubleSpinBox {{ background-color: {C['input']};"
            f"  color: {C['text']}; border: 1px solid {C['border']};"
            f"  border-radius: 3px; padding: 0px 2px; }}"
            f"QSpinBox::up-button, QDoubleSpinBox::up-button"
            f"  {{ subcontrol-origin: border; subcontrol-position: top right;"
            f"  background-color: {C['selection']}; border: 1px solid {C['border']}; }}"
            f"QSpinBox::down-button, QDoubleSpinBox::down-button"
            f"  {{ subcontrol-origin: border; subcontrol-position: bottom right;"
            f"  background-color: {C['selection']}; border: 1px solid {C['border']}; }}"
            f"QSpinBox::up-arrow, QDoubleSpinBox::up-arrow"
            f"  {{ image: url({C['arrow_dir']}/up.png); width: 10px; height: 8px; }}"
            f"QSpinBox::down-arrow, QDoubleSpinBox::down-arrow"
            f"  {{ image: url({C['arrow_dir']}/down.png); width: 10px; height: 8px; }}"
            # Line edits — padding: 3px restores 26px height
            f"QLineEdit {{ background-color: {C['input']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px;"
            f"  padding: 3px; }}"
            # List widgets (multi_enum)
            f"QListWidget {{ background-color: {C['input']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px; }}"
            f"QListWidget::item:selected {{ background-color: {C['selection']};"
            f"  color: {C['text']}; }}"
            # Group boxes
            f"QGroupBox {{ color: {C['text']}; border: 1px solid {C['border']};"
            f"  border-radius: 4px; margin-top: 18px; padding-top: 4px; }}"
            f"QGroupBox::title {{ color: {C['text']};"
            f"  subcontrol-origin: margin; padding: 2px 4px 0 4px; left: 8px; }}"
            # Table (tool picker)
            f"QTableWidget {{ background-color: {C['input']}; color: {C['text']};"
            f"  gridline-color: {C['border']}; }}"
            f"QTableWidget::item:selected {{ background-color: {C['selection']};"
            f"  color: {C['text']}; }}"
            f"QHeaderView::section {{ background-color: {C['widget']};"
            f"  color: {C['text']}; border: 1px solid {C['border']}; padding: 4px; }}"
            # NOTE: Scrollbars are NOT styled via QSS — doing so replaces the
            # native renderer and loses smooth animations, hover expansion, and
            # arrow buttons.  Instead, setColorScheme(Dark) tells the platform
            # to render native controls (including scrollbars) in dark mode.
            # Buttons
            f"QPushButton {{ background-color: {C['widget']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px;"
            f"  padding: 4px 12px; }}"
            f"QPushButton:hover {{ background-color: {C['selection']}; }}"
            f"QPushButton:pressed {{ background-color: {C['input']}; }}"
            # Plain text edits (extra flags, text-type fields)
            f"QPlainTextEdit {{ background-color: {C['input']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px;"
            f"  padding: 1px; }}"
            # Tooltips
            f"QToolTip {{ background-color: {C['widget']}; color: {C['text']};"
            f"  border: 1px solid {C['border']}; border-radius: 3px; }}"
            # Status bar
            f"QStatusBar {{ color: {C['text']}; }}"
        )
    else:
        # Restore native light color scheme for platform controls.
        try:
            app.styleHints().setColorScheme(Qt.ColorScheme.Light)
        except AttributeError:
            pass
        app.setPalette(_original_palette)
        # Force readable tooltip colors regardless of OS theme.
        # On Linux with mixed-theme desktops, _original_palette can have
        # unreadable tooltip color combinations.
        light_palette = app.palette()
        light_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#f5f5f5"))
        light_palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#1a1a1a"))
        app.setPalette(light_palette)
        app.setStyleSheet(
            "QToolTip { background-color: #f5f5f5; color: #1a1a1a;"
            " border: 1px solid #c0c0c0; border-radius: 3px; padding: 2px; }"
        )


def _invalid_style() -> str:
    """Return a QSS border style string for invalid/failed-validation fields."""
    c = DARK_COLORS["required"]
    return f"border: 1px solid {c};" if _dark_mode else "border: 1px solid red;"


def _required_color() -> str:
    return DARK_COLORS["required"] if _dark_mode else "red"


class DragHandle(QWidget):
    """A small centered grip bar that the user drags vertically to resize the output panel."""

    HEIGHT = 8
    LINE_WIDTH = 32
    LINE_SPACING = 3

    def __init__(self, target: QWidget, settings: QSettings, parent=None):
        super().__init__(parent)
        self._target = target
        self._settings = settings
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_h = 0
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
        self.setToolTip("Drag to resize output panel")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(DARK_COLORS["disabled"] if _dark_mode else "#888888")
        p.setPen(QPen(color, 1))
        cx = self.width() / 2
        cy = self.height() / 2
        x0 = int(cx - self.LINE_WIDTH / 2)
        x1 = int(cx + self.LINE_WIDTH / 2)
        y_top = int(cy - self.LINE_SPACING / 2)
        y_bot = int(cy + self.LINE_SPACING / 2)
        p.drawLine(x0, y_top, x1, y_top)
        p.drawLine(x0, y_bot, x1, y_bot)
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start_y = event.globalPosition().y()
            self._drag_start_h = self._target.height()
            event.accept()

    def _effective_max_height(self) -> int:
        """Return the effective maximum output height, accounting for visible sibling widgets."""
        win = self._target.window()
        if win is None:
            screen = QApplication.primaryScreen()
            return screen.availableGeometry().height() if screen else 2000
        parent = self._target.parentWidget()
        if parent is None:
            return win.height() // 2
        layout = parent.layout()
        sibling_total = 0
        if layout is not None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                w = item.widget() if item is not None else None
                if w is None or w is self._target:
                    continue
                if not w.isVisible():
                    continue
                sh = w.sizeHint().height()
                if sh > 0:
                    sibling_total += min(w.height(), sh)
                else:
                    sibling_total += w.height()
        margin = 24  # safety for layout spacing and margins
        available = parent.height() - sibling_total - margin
        if available < OUTPUT_MIN_HEIGHT:
            available = OUTPUT_MIN_HEIGHT
        return available

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            delta = int(self._drag_start_y - event.globalPosition().y())
            new_h = max(OUTPUT_MIN_HEIGHT,
                        min(self._effective_max_height(),
                            self._drag_start_h + delta))
            sb = self._target.verticalScrollBar()
            was_at_bottom = sb.value() >= sb.maximum() - 1
            old_value = sb.value()
            self._target.setFixedHeight(new_h)
            self._target.updateGeometry()
            if was_at_bottom:
                sb.setValue(sb.maximum())
            else:
                sb.setValue(old_value)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self._settings.setValue("output/height", self._target.height())
            event.accept()


class ToolForm(QWidget):
    """Dynamically renders a GUI form from a validated, normalized tool dict."""

    command_changed = Signal()

    # Scope constant for global (top-level) arguments
    GLOBAL = "__global__"

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = normalize_tool(data)
        # (scope, flag) -> {arg, widget, label, repeat_spin}
        self.fields = {}
        # (scope, group_name) -> list of (scope, flag) keys
        self.groups = {}
        # (scope, flag) -> compiled regex
        self.validators = {}
        # scope -> {display_group_name: QGroupBox}
        self.display_groups = {}

        self._build_ui()
        self._apply_groups()
        self._apply_dependencies()

    def eventFilter(self, obj, event) -> bool:
        """Handle key events on the search bar and clicks on display_group titles."""
        # Display group collapse/expand via title click
        if (
            isinstance(obj, QGroupBox)
            and obj.property("_dg_content") is not None
            and event.type() == event.Type.MouseButtonPress
            and event.position().y() < 20
        ):
            self._toggle_display_group(obj)
            return True
        if obj is self._search_bar and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._search_prev()
                else:
                    self._search_next()
                return True
            if key == Qt.Key.Key_Escape:
                self.close_search()
                return True
        return super().eventFilter(obj, event)

    def _field_key(self, scope: str, flag: str) -> tuple:
        """Return the canonical (scope, flag) key used to look up a field."""
        return (scope, flag)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build and lay out all widgets for the form."""
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        header = QLabel(self.data["tool"])
        header.setStyleSheet("font-size: 16px; font-weight: bold;")
        root.addWidget(header)

        if self.data["description"]:
            desc = QLabel(self.data["description"])
            desc.setWordWrap(True)
            root.addWidget(desc)

        # Separator between header and options
        self._header_sep = QFrame()
        self._header_sep.setFrameShape(QFrame.Shape.HLine)
        self._header_sep.setFrameShadow(QFrame.Shadow.Plain)
        color = DARK_COLORS["border"] if _dark_mode else "#999999"
        self._header_sep.setStyleSheet(
            f"QFrame {{ color: {color}; max-height: 1px; margin: 4px 0 2px 0; }}"
        )
        root.addWidget(self._header_sep)

        # Field search bar (always visible, Ctrl+F focuses it)
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Find field...  (Ctrl+F)")
        self._search_bar.textChanged.connect(self._on_search_text_changed)
        self._search_bar.installEventFilter(self)
        self._search_matches = []
        self._search_index = -1

        self._search_no_match_label = QLabel("No matches")
        err_color = DARK_COLORS["error"] if _dark_mode else "red"
        self._search_no_match_label.setStyleSheet(f"color: {err_color}; font-style: italic; margin-left: 4px;")
        self._search_no_match_label.setVisible(False)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(self._search_bar, 1)
        search_row.addWidget(self._search_no_match_label)
        self._search_row_widget = QWidget()
        self._search_row_widget.setLayout(search_row)
        root.addWidget(self._search_row_widget)

        # Elevation control
        self.elevation_check = None
        elevated = self.data.get("elevated")
        if elevated in ("optional", "always") and not _check_already_elevated():
            elev_row = QHBoxLayout()
            self.elevation_check = QCheckBox(_elevation_label())
            if elevated == "always":
                self.elevation_check.setChecked(True)
                note = QLabel("This tool requires elevated privileges to function.")
            else:
                note = QLabel("Some features of this tool may require elevated privileges.")
            note.setWordWrap(True)
            note_color = DARK_COLORS["text_dim"] if _dark_mode else "gray"
            note.setStyleSheet(f"color: {note_color}; font-style: italic;")
            self._elevation_note = note
            elev_row.addWidget(self.elevation_check)
            elev_row.addWidget(note, 1)
            root.addLayout(elev_row)
            self.elevation_check.stateChanged.connect(lambda _: self.command_changed.emit())

        # Subcommand selector
        self.sub_combo = None
        if self.data["subcommands"]:
            row = QHBoxLayout()
            row.addWidget(QLabel("Subcommand:"))
            self.sub_combo = QComboBox()
            for sub in self.data["subcommands"]:
                label = sub["name"]
                full_desc = sub.get("description", "")
                if full_desc:
                    max_desc = 80 - len(label) - 5  # 5 for "  —  " separator
                    desc = full_desc
                    if max_desc > 20 and len(desc) > max_desc:
                        desc = desc[:max_desc - 1].rstrip() + "…"
                    if max_desc > 20:
                        label += f"  —  {desc}"
                self.sub_combo.addItem(label, sub["name"])
                if full_desc:
                    idx = self.sub_combo.count() - 1
                    self.sub_combo.setItemData(idx, f"<p>{full_desc}</p>", Qt.ItemDataRole.ToolTipRole)
            self.sub_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.sub_combo.setMinimumWidth(200)
            self.sub_combo.setMaximumWidth(600)
            row.addWidget(self.sub_combo, 1)
            root.addLayout(row)
            self.sub_combo.currentIndexChanged.connect(self._on_subcommand_changed)

            def _update_sub_combo_tooltip():
                idx = self.sub_combo.currentIndex()
                tip = self.sub_combo.itemData(idx, Qt.ItemDataRole.ToolTipRole) or ""
                self.sub_combo.setToolTip(tip)

            self.sub_combo.currentIndexChanged.connect(_update_sub_combo_tooltip)
            _update_sub_combo_tooltip()

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_widget)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll.setWidget(scroll_widget)
        root.addWidget(self._scroll, 1)

        # Global args section
        if self.data["arguments"]:
            if self.data["subcommands"]:
                box = QGroupBox("Global Options")
                box_layout = QFormLayout()
                box.setLayout(box_layout)
                self._add_args(self.data["arguments"], box_layout, self.GLOBAL)
                self.scroll_layout.addWidget(box)
            else:
                form = QFormLayout()
                self._add_args(self.data["arguments"], form, self.GLOBAL)
                self.scroll_layout.addLayout(form)

        # Subcommand args sections (stacked, toggle visibility)
        self.sub_sections = []
        if self.data["subcommands"]:
            for sub in self.data["subcommands"]:
                box = QGroupBox(f"{sub['name']} Options")
                box_layout = QFormLayout()
                box.setLayout(box_layout)
                self._add_args(sub["arguments"], box_layout, sub["name"])
                self.scroll_layout.addWidget(box)
                self.sub_sections.append(box)
            self._on_subcommand_changed(0)

        # Additional Flags
        self._build_extra_flags()

        self.scroll_layout.addStretch()

    def _build_extra_flags(self) -> None:
        """Build the collapsible 'Additional Flags' free-text section."""
        group = QGroupBox("Additional Flags")
        group.setCheckable(True)
        group.setChecked(False)
        layout = QVBoxLayout()
        self.extra_flags_edit = QPlainTextEdit()
        self.extra_flags_edit.setMaximumHeight(TEXT_WIDGET_HEIGHT)
        self.extra_flags_edit.setToolTip(
            "<p>Raw flags appended directly to the command. "
            "Use this for flags not covered by the form above.</p>"
        )
        self.extra_flags_edit.textChanged.connect(self._validate_extra_flags)
        self.extra_flags_edit.textChanged.connect(lambda: self.command_changed.emit())
        layout.addWidget(self.extra_flags_edit)
        group.setLayout(layout)
        self.scroll_layout.addWidget(group)
        self.extra_flags_group = group
        group.toggled.connect(lambda _: self.command_changed.emit())

    def _validate_extra_flags(self) -> None:
        """Show a red border on the extra flags field if shlex parsing fails."""
        text = self.extra_flags_edit.toPlainText().strip()
        if not text:
            self.extra_flags_edit.setStyleSheet("")
            return
        try:
            shlex.split(text)
            self.extra_flags_edit.setStyleSheet("")
        except ValueError:
            self.extra_flags_edit.setStyleSheet(_invalid_style())

    def _add_args(self, args: list, form_layout: QFormLayout, scope: str) -> None:
        """Create and register a widget row for each arg in args under the given scope."""
        # Partition args by display_group while preserving order
        ungrouped = []
        display_groups: dict[str, list] = {}
        display_group_order: list[str] = []
        for arg in args:
            dg = arg.get("display_group")
            if dg:
                if dg not in display_groups:
                    display_groups[dg] = []
                    display_group_order.append(dg)
                display_groups[dg].append(arg)
            else:
                ungrouped.append(arg)

        # Add ungrouped args directly to the form layout
        for arg in ungrouped:
            self._add_single_arg(arg, form_layout, scope)

        # Add each display_group as a collapsible section
        for dg_name in display_group_order:
            dg_args = display_groups[dg_name]
            box = QGroupBox(f"\u25be {dg_name}")
            box_inner = QWidget()
            box_form = QFormLayout(box_inner)
            box_form.setContentsMargins(0, 0, 0, 0)
            box_layout = QVBoxLayout()
            box_layout.setContentsMargins(4, 4, 4, 4)
            box_layout.addWidget(box_inner)
            box.setLayout(box_layout)

            for arg in dg_args:
                self._add_single_arg(arg, box_form, scope)

            # Make the group box collapsible via title click
            box.setProperty("_dg_content", box_inner)
            box.setProperty("_dg_collapsed", False)
            box.setProperty("_dg_name", dg_name)
            box.installEventFilter(self)
            box.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

            # Store in parent layout — form_layout is a QFormLayout,
            # so wrap the group box in a row spanning both columns.
            form_layout.addRow(box)

            self.display_groups.setdefault(scope, {})[dg_name] = box

    def _toggle_display_group(self, box: QGroupBox) -> None:
        """Toggle visibility of a collapsible display group's contents."""
        content = box.property("_dg_content")
        if content is not None:
            collapsed = box.property("_dg_collapsed")
            collapsed = not collapsed
            box.setProperty("_dg_collapsed", collapsed)
            content.setVisible(not collapsed)
            # Update collapse/expand indicator in title
            name = box.property("_dg_name")
            if name:
                box.setTitle(f"\u25b8 {name}" if collapsed else f"\u25be {name}")

    # ------------------------------------------------------------------
    # Field search (Ctrl+F)
    # ------------------------------------------------------------------

    def open_search(self) -> None:
        """Focus the field search bar and select all text."""
        self._search_bar.setFocus()
        self._search_bar.selectAll()

    def close_search(self) -> None:
        """Clear search text, highlights, and defocus the search bar."""
        self._search_bar.clear()
        self._search_no_match_label.setVisible(False)
        self._clear_search_highlights()
        self._search_matches = []
        self._search_index = -1
        self._search_bar.clearFocus()

    def _on_search_text_changed(self, text: str) -> None:
        """Called when search bar text changes. Find all matches and highlight first."""
        self._clear_search_highlights()
        self._search_matches = []
        self._search_index = -1
        query = text.strip().lower()
        if not query:
            self._search_no_match_label.setVisible(False)
            return

        # Determine visible scopes
        visible_scopes = {self.GLOBAL}
        current_sub = self.get_current_subcommand()
        if current_sub:
            visible_scopes.add(current_sub)

        for key, field in self.fields.items():
            scope, flag = key
            if scope not in visible_scopes:
                continue
            arg = field["arg"]
            name_lower = arg["name"].lower()
            flag_lower = flag.lower()
            desc_lower = arg.get("description", "").lower()
            if query in name_lower or query in flag_lower or query in desc_lower:
                self._search_matches.append(key)

        if self._search_matches:
            self._search_no_match_label.setVisible(False)
            self._search_index = 0
            self._highlight_and_scroll(self._search_matches[0])
        else:
            self._search_no_match_label.setVisible(True)

    def _search_next(self) -> None:
        """Jump to the next search match (Enter key)."""
        if not self._search_matches:
            return
        self._clear_search_highlights()
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._highlight_and_scroll(self._search_matches[self._search_index])

    def _search_prev(self) -> None:
        """Jump to the previous search match (Shift+Enter)."""
        if not self._search_matches:
            return
        self._clear_search_highlights()
        self._search_index = (self._search_index - 1) % len(self._search_matches)
        self._highlight_and_scroll(self._search_matches[self._search_index])

    def _highlight_and_scroll(self, key: tuple) -> None:
        """Highlight the matching field's label and scroll it into view."""
        field = self.fields[key]
        label = field["label"]

        # Expand any collapsed display_group containing this field
        scope = key[0]
        if scope in self.display_groups:
            for dg_name, box in self.display_groups[scope].items():
                if box.isAncestorOf(label) and box.property("_dg_collapsed"):
                    self._toggle_display_group(box)

        # Apply highlight — theme-aware
        highlight = DARK_COLORS["selection"] if _dark_mode else "#fff176"
        label.setStyleSheet(f"background-color: {highlight};")
        label.setProperty("_search_highlighted", True)

        # Scroll into view
        self._scroll.ensureWidgetVisible(label, 50, 50)

    def _clear_search_highlights(self) -> None:
        """Remove search highlight from all labels."""
        for field in self.fields.values():
            label = field["label"]
            if label.property("_search_highlighted"):
                label.setStyleSheet("")
                label.setProperty("_search_highlighted", False)

    def _add_single_arg(self, arg: dict, form_layout: QFormLayout, scope: str) -> None:
        """Create and register a single widget row for an arg under the given scope."""
        flag = arg["flag"]
        key = self._field_key(scope, flag)
        widget = self._build_widget(arg, key)
        label_text = html.escape(arg["name"])
        if arg["required"]:
            label_text = f"<b>{label_text} <span style='color:{_required_color()};'>*</span></b>"
        # Dangerous: prepend red warning symbol
        if arg.get("dangerous"):
            warn_color = DARK_COLORS["required"] if _dark_mode else "red"
            label_text = f"<span style='color:{warn_color};'>\u26a0</span> {label_text}"
        # Deprecated: strikethrough + colored suffix
        if arg.get("deprecated"):
            dep_color = DARK_COLORS["warning_text"] if _dark_mode else "#856404"
            label_text = f"<s>{label_text}</s> <span style='color:{dep_color};'>(deprecated)</span>"
        label = QLabel(label_text)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setToolTip(self._build_tooltip(arg))

        # Repeatable: add a count spinner next to the widget
        repeat_spin = None
        if arg["repeatable"] and arg["type"] == "boolean":
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(widget, 1)
            repeat_spin = QSpinBox()
            repeat_spin.setRange(1, REPEAT_SPIN_MAX)
            repeat_spin.setValue(1)
            repeat_spin.setPrefix("x")
            repeat_spin.setToolTip("Number of times to repeat this flag")
            repeat_spin.setMaximumWidth(REPEAT_SPIN_WIDTH)
            repeat_spin.valueChanged.connect(lambda _: self.command_changed.emit())
            row_layout.addWidget(repeat_spin)
            form_layout.addRow(label, row_widget)
        else:
            form_layout.addRow(label, widget)

        if arg["group"]:
            group_key = (scope, arg["group"])
            self.groups.setdefault(group_key, []).append(key)

        self.fields[key] = {
            "arg": arg,
            "widget": widget,
            "label": label,
            "repeat_spin": repeat_spin,
        }

    # ------------------------------------------------------------------
    # Widget factory
    # ------------------------------------------------------------------

    def _build_widget(self, arg: dict, key: tuple) -> QWidget:
        """Instantiate and return the appropriate widget for the given arg's type."""
        try:
            return self._build_widget_inner(arg, key)
        # broadly defensive — widget construction can raise AttributeError/RuntimeError/TypeError/ValueError/re.error/IndexError/KeyError; fallback widget preserves the form even for unanticipated schema shapes.
        except Exception as exc:
            import traceback
            print(f"Warning: failed to render widget for \"{arg.get('name', '?')}\" "
                  f"(type \"{arg.get('type', '?')}\"): {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            w = QLineEdit()
            w._is_fallback = True
            w.setToolTip(f"<p>This field could not be rendered as '{html.escape(str(arg.get('type', '?')))}' "
                         f"-- using text input as fallback</p>")
            w.textChanged.connect(lambda _: self.command_changed.emit())
            return w

    def _build_widget_inner(self, arg: dict, key: tuple) -> QWidget:
        """Instantiate and return the appropriate widget for the given arg's type."""
        t = arg["type"]

        if t == "boolean":
            w = QCheckBox()
            if arg["default"] is True:
                w.setChecked(True)
            w.stateChanged.connect(lambda _: self.command_changed.emit())

        elif t == "string":
            examples = arg.get("examples")
            if examples:
                # Editable combo with suggestions
                w = QComboBox()
                w.setEditable(True)
                w.addItem("")  # empty first item so nothing is pre-selected
                w.addItems(examples)
                if arg["default"] is not None:
                    w.setCurrentText(str(arg["default"]))
                else:
                    w.setCurrentText("")
                if arg["description"]:
                    w.lineEdit().setPlaceholderText(arg["description"])
                if arg["validation"]:
                    regex = re.compile(arg["validation"])
                    self.validators[key] = regex
                    w.lineEdit().textChanged.connect(
                        lambda text, ww=w.lineEdit(), rx=regex: self._validate_input(ww, rx, text)
                    )
                w.currentTextChanged.connect(lambda _: self.command_changed.emit())
            else:
                # Plain line edit
                w = QLineEdit()
                if arg["default"] is not None:
                    w.setText(str(arg["default"]))
                if arg["description"]:
                    w.setPlaceholderText(arg["description"])
                if arg["validation"]:
                    regex = re.compile(arg["validation"])
                    self.validators[key] = regex
                    w.textChanged.connect(lambda text, ww=w, rx=regex: self._validate_input(ww, rx, text))
                w.textChanged.connect(lambda _: self.command_changed.emit())

        elif t == "password":
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            line = QLineEdit()
            line.setEchoMode(QLineEdit.EchoMode.Password)
            if arg["default"] is not None:
                line.setText(str(arg["default"]))
            if arg["description"]:
                line.setPlaceholderText(arg["description"])
            if arg["validation"]:
                regex = re.compile(arg["validation"])
                self.validators[key] = regex
                line.textChanged.connect(lambda text, ww=line, rx=regex: self._validate_input(ww, rx, text))
            line.textChanged.connect(lambda _: self.command_changed.emit())
            show_cb = QCheckBox("Show")
            show_cb.toggled.connect(
                lambda checked, le=line: le.setEchoMode(
                    QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
                )
            )
            layout.addWidget(line, 1)
            layout.addWidget(show_cb)
            container._line_edit = line
            container._show_toggle = show_cb
            w = container

        elif t == "text":
            w = QPlainTextEdit()
            w.setMaximumHeight(TEXT_WIDGET_HEIGHT)
            if arg["default"] is not None:
                w.setPlainText(str(arg["default"]))
            w.textChanged.connect(lambda: self.command_changed.emit())

        elif t == "integer":
            w = self._build_numeric_spinbox(arg, integer_type=True)

        elif t == "float":
            w = self._build_numeric_spinbox(arg, integer_type=False)

        elif t == "enum":
            w = QComboBox()
            if not arg["required"]:
                w.addItem("", "")
            for choice in (arg["choices"] or []):
                w.addItem(choice, choice)
            if arg["default"] is not None:
                idx = w.findData(str(arg["default"]))
                if idx >= 0:
                    w.setCurrentIndex(idx)
            w.currentIndexChanged.connect(lambda _: self.command_changed.emit())

        elif t == "multi_enum":
            w = QListWidget()
            w.setMaximumHeight(MULTI_ENUM_HEIGHT)
            for choice in (arg["choices"] or []):
                item = QListWidgetItem(choice)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                w.addItem(item)
            w.itemChanged.connect(lambda: self.command_changed.emit())

        elif t == "file":
            w = self._build_path_widget(arg, pick_directory=False)

        elif t == "directory":
            w = self._build_path_widget(arg, pick_directory=True)

        else:
            w = QLabel(f"[unsupported type: {t}]")

        if arg["description"]:
            w.setToolTip(self._build_tooltip(arg))

        return w

    def _build_numeric_spinbox(self, arg: dict, *, integer_type: bool) -> "QAbstractSpinBox":
        """Build a QSpinBox (integer) or QDoubleSpinBox (float) from a numeric arg.

        The two types share all structural setup — range, sentinel handling for
        null defaults, min/max clamping, valueChanged wiring. The only behavior
        differences are (1) the widget class, (2) the numeric cast, and (3)
        setDecimals(2) for floats. This helper consolidates both paths.
        """
        cls = QSpinBox if integer_type else QDoubleSpinBox
        cast = int if integer_type else float
        sentinel = -1 if integer_type else -1.0
        w = cls()
        w.setRange(-SPINBOX_RANGE, SPINBOX_RANGE)
        if not integer_type:
            w.setDecimals(2)
        if arg["default"] is not None:
            w.setValue(cast(arg["default"]))
        else:
            w.setRange(sentinel, SPINBOX_RANGE)
            w.setValue(sentinel)
            w.setSpecialValueText(" ")
        if arg.get("min") is not None:
            if arg["default"] is None:
                # Sentinel is one below the schema min
                w.setMinimum(cast(arg["min"]) - cast(1))
                w.setValue(cast(arg["min"]) - cast(1))
            else:
                w.setMinimum(cast(arg["min"]))
        if arg.get("max") is not None:
            w.setMaximum(cast(arg["max"]))
        w.valueChanged.connect(lambda _: self.command_changed.emit())
        return w

    def _build_path_widget(self, arg: dict, *, pick_directory: bool) -> "QWidget":
        """Build a QWidget(QLineEdit + Browse button) for a file or directory arg.

        Both path types share the full layout shape; the only difference is
        which module-scope browse helper the button invokes. This helper
        consolidates both paths.
        """
        browse_fn = _browse_directory if pick_directory else _browse_file
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        line = QLineEdit()
        if arg["default"] is not None:
            line.setText(str(arg["default"]))
        if arg["description"]:
            line.setPlaceholderText(arg["description"])
        btn = QPushButton("Browse...")
        btn.clicked.connect(lambda checked, le=line: browse_fn(le, self))
        layout.addWidget(line, 1)
        layout.addWidget(btn)
        line.textChanged.connect(lambda _: self.command_changed.emit())
        w._line_edit = line
        return w

    @staticmethod
    def _build_tooltip(arg: dict) -> str:
        """Build a structured tooltip showing flag, type, description, and validation."""
        _esc = html.escape
        warning_lines = []
        if arg.get("deprecated"):
            warning_lines.append(f"\u26a0 DEPRECATED: {_esc(str(arg['deprecated']))}")
        if arg.get("dangerous"):
            warning_lines.append("\u26a0 CAUTION: This flag may have destructive or irreversible effects.")
        parts = [_esc(arg["flag"])]
        if arg.get("short_flag"):
            parts.append(_esc(arg["short_flag"]))
        type_info = arg["type"]
        sep = arg.get("separator", "space")
        if sep == "equals" or (sep == "none" and arg["type"] != "boolean"):
            type_info += f", separator: {sep}"
        header = f"{' '.join(parts)} ({type_info})"
        lines = warning_lines + [""] + [header] if warning_lines else [header]
        if arg.get("description") or arg.get("validation"):
            lines.append("")
        if arg.get("description"):
            lines.append(_esc(arg["description"]))
        if arg.get("validation"):
            lines.append(f"Validation: {_esc(arg['validation'])}")
        return "<p>" + "<br>".join(lines) + "</p>"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_input(self, widget: QLineEdit, regex, text: str) -> None:
        """Apply a red border to widget if text is non-empty and fails regex."""
        if text and not regex.search(text):
            widget.setStyleSheet(_invalid_style())
        else:
            widget.setStyleSheet("")

    # ------------------------------------------------------------------
    # Group exclusivity
    # ------------------------------------------------------------------

    def _apply_groups(self) -> None:
        """Wire up mutual-exclusivity signals for all groups."""
        for group_key, field_keys in self.groups.items():
            for fk in field_keys:
                field = self.fields[fk]
                widget = field["widget"]
                if isinstance(widget, QCheckBox):
                    widget.stateChanged.connect(
                        lambda state, active=fk, gk=group_key: self._on_group_toggled(gk, active, state)
                    )

    def _on_group_toggled(self, group_key: tuple, active_key: tuple, state: int) -> None:
        """Uncheck all other members of a mutual-exclusivity group when one is checked."""
        if state != Qt.CheckState.Checked.value:
            return
        for fk in self.groups[group_key]:
            if fk != active_key:
                field = self.fields[fk]
                w = field["widget"]
                if isinstance(w, QCheckBox):
                    w.blockSignals(True)
                    w.setChecked(False)
                    w.blockSignals(False)
        self.command_changed.emit()

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------

    def _apply_dependencies(self) -> None:
        """Wire up enable/disable signals for all fields that have a depends_on parent."""
        for key, field in self.fields.items():
            dep_flag = field["arg"]["depends_on"]
            if not dep_flag:
                continue
            # Look for parent in the same scope first, then global
            scope = key[0]
            parent_key = self._field_key(scope, dep_flag)
            if parent_key not in self.fields:
                parent_key = self._field_key(self.GLOBAL, dep_flag)
            if parent_key not in self.fields:
                name = field["arg"].get("name", "?")
                print(f"Warning: dependency wiring failed for \"{name}\" — "
                      f"parent \"{dep_flag}\" not found in form. "
                      f"Field left enabled (fail-open).", file=sys.stderr)
                continue
            parent_field = self.fields[parent_key]
            # Set initial disabled state
            field["widget"].setEnabled(self._is_field_active(parent_key))
            field["label"].setEnabled(self._is_field_active(parent_key))
            # Connect parent changes
            self._connect_dependency(parent_key, parent_field, child_field=field)

    def _connect_dependency(self, parent_key: tuple, parent_field: dict, child_field: dict) -> None:
        """Connect the appropriate change signal on the parent widget to update the child's enabled state."""
        pw = parent_field["widget"]
        if isinstance(pw, QCheckBox):
            pw.stateChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        elif isinstance(pw, QLineEdit):
            pw.textChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        elif isinstance(pw, QPlainTextEdit):
            pw.textChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        elif isinstance(pw, QComboBox):
            if pw.isEditable():
                pw.currentTextChanged.connect(
                    lambda: self._update_dependent(parent_key, child_field)
                )
            else:
                pw.currentIndexChanged.connect(
                    lambda: self._update_dependent(parent_key, child_field)
                )
        elif isinstance(pw, QSpinBox):
            pw.valueChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        elif isinstance(pw, QDoubleSpinBox):
            pw.valueChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        elif isinstance(pw, QListWidget):
            pw.itemChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )
        # file/directory containers — connect the inner QLineEdit
        elif hasattr(pw, '_line_edit'):
            pw._line_edit.textChanged.connect(
                lambda: self._update_dependent(parent_key, child_field)
            )

    def _update_dependent(self, parent_key: tuple, child_field: dict) -> None:
        """Enable or disable a dependent field based on the parent's current value."""
        active = self._is_field_active(parent_key)
        child_field["widget"].setEnabled(active)
        child_field["label"].setEnabled(active)
        self.command_changed.emit()

    def _is_field_active(self, key: tuple) -> bool:
        """Return True if the given field's widget has a meaningful value."""
        field = self.fields[key]
        w = field["widget"]
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QLineEdit):
            return bool(w.text().strip())
        if isinstance(w, QPlainTextEdit):
            return bool(w.toPlainText().strip())
        if isinstance(w, QComboBox):
            if w.isEditable():
                return bool(w.currentText().strip())
            return bool(w.currentData())
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            if w.specialValueText() and w.value() == w.minimum():
                return False
            return True
        if isinstance(w, QListWidget):
            for i in range(w.count()):
                if w.item(i).checkState() == Qt.CheckState.Checked:
                    return True
            return False
        if hasattr(w, '_line_edit'):
            return bool(w._line_edit.text().strip())
        return False

    # ------------------------------------------------------------------
    # Subcommand switching
    # ------------------------------------------------------------------

    def _on_subcommand_changed(self, index: int) -> None:
        """Show only the selected subcommand's options section."""
        for i, section in enumerate(self.sub_sections):
            section.setVisible(i == index)
        self._scroll.verticalScrollBar().setValue(0)
        self.command_changed.emit()

    # ------------------------------------------------------------------
    # Value reading (for command assembly)
    # ------------------------------------------------------------------

    def get_current_subcommand(self) -> str | None:
        """Return the currently selected subcommand name, or None if no subcommands."""
        if self.sub_combo is not None:
            return self.sub_combo.currentData()
        return None

    def extra_flags_valid(self) -> bool:
        """Return True if extra flags are disabled, empty, or parse cleanly with shlex."""
        if not self.extra_flags_group.isChecked():
            return True
        text = self.extra_flags_edit.toPlainText().strip()
        if not text:
            return True
        try:
            shlex.split(text)
            return True
        except ValueError:
            return False

    def get_extra_flags(self) -> list[str]:
        """Return the parsed extra-flags tokens, or an empty list if the section is disabled."""
        if not self.extra_flags_group.isChecked():
            return []
        text = self.extra_flags_edit.toPlainText().strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except ValueError:
            return []

    def _read_field_value(self, key: tuple, *, respect_enabled: bool = True) -> int | str | list | None:
        """Core field-value reader used by both get_field_value and _raw_field_value.

        Args:
            key: (scope, flag) tuple identifying the field.
            respect_enabled: If True, return None for disabled widgets.
        """
        field = self.fields.get(key)
        if not field:
            return None
        w = field["widget"]
        if respect_enabled and not w.isEnabled():
            return None
        arg = field["arg"]
        t = arg["type"]

        # Fallback widget — treat as plain string regardless of declared type
        if getattr(w, "_is_fallback", False):
            v = w.text().strip()
            return v if v else None

        if t == "boolean":
            if isinstance(w, QCheckBox) and w.isChecked():
                if field["repeat_spin"]:
                    return field["repeat_spin"].value()
                if respect_enabled:
                    return 1
                return True
            return None

        elif t == "string":
            v = w.currentText().strip() if isinstance(w, QComboBox) else w.text().strip()
            return v if v else None

        elif t == "password":
            v = w._line_edit.text()
            return v if v else None

        elif t == "text":
            v = w.toPlainText().strip()
            return v if v else None

        elif t == "integer":
            if w.specialValueText() and w.value() == w.minimum():
                return None
            return w.value()

        elif t == "float":
            if w.specialValueText() and w.value() == w.minimum():
                return None
            return w.value()

        elif t == "enum":
            v = w.currentData()
            return v if v else None

        elif t == "multi_enum":
            selected = []
            for i in range(w.count()):
                item = w.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    selected.append(item.text())
            return selected if selected else None

        elif t in ("file", "directory"):
            v = w._line_edit.text().strip()
            return v if v else None

        return None

    def get_field_value(self, key: tuple) -> int | str | list | None:
        """Return the current widget value for a field key, or None if empty/unchecked/disabled."""
        return self._read_field_value(key, respect_enabled=True)

    # ------------------------------------------------------------------
    # Preset serialization
    # ------------------------------------------------------------------

    def _raw_field_value(self, key: tuple) -> int | str | list | None:
        """Like get_field_value but ignores enabled state (reads widget regardless)."""
        return self._read_field_value(key, respect_enabled=False)

    def serialize_values(self) -> dict:
        """Serialize all current field values to a flat dict for preset storage."""
        preset = {}
        preset["_format"] = "scaffold_preset"
        preset["_tool"] = self.data["tool"]
        preset["_subcommand"] = self.get_current_subcommand()
        preset["_schema_hash"] = schema_hash(self.data)
        if self.elevation_check is not None:
            preset["_elevated"] = self.elevation_check.isChecked()
        extra_text = self.extra_flags_edit.toPlainText().strip()
        if self.extra_flags_group.isChecked() and extra_text:
            preset["_extra_flags"] = extra_text

        for key, field in self.fields.items():
            if field["arg"]["type"] == "password":
                continue
            value = self._raw_field_value(key)
            if value is not None:
                # Use scope:flag as key to avoid collisions across subcommands
                scope, flag = key
                if scope == self.GLOBAL:
                    preset[flag] = value
                else:
                    preset[f"{scope}:{flag}"] = value

        return preset

    def apply_values(self, preset: dict) -> bool:
        """Apply a preset dict to the form, resetting unmentioned fields to defaults.

        Returns True if the current schema contains any password fields (which are never saved and must be re-entered after any preset/history restore).
        """
        self.blockSignals(True)

        # Subcommand
        sub = preset.get("_subcommand")
        if self.sub_combo is not None:
            idx = self.sub_combo.findData(sub) if sub else -1
            self.sub_combo.setCurrentIndex(idx if idx >= 0 else 0)

        # Elevation
        if self.elevation_check is not None and "_elevated" in preset:
            self.elevation_check.setChecked(bool(preset["_elevated"]))

        # Extra flags
        extra = preset.get("_extra_flags", "")
        if extra:
            self.extra_flags_group.setChecked(True)
            self.extra_flags_edit.setPlainText(extra)
        else:
            self.extra_flags_group.setChecked(False)
            self.extra_flags_edit.clear()

        # Apply field values — reset everything first, then set from preset
        had_passwords = False
        for key, field in self.fields.items():
            if field["arg"]["type"] == "password":
                had_passwords = True
                continue
            scope, flag = key
            if scope == self.GLOBAL:
                preset_key = flag
            else:
                preset_key = f"{scope}:{flag}"
            value = preset.get(preset_key)
            if value is None:
                arg = field["arg"]
                if arg["type"] == "boolean":
                    value = True if arg["default"] is True else None
                else:
                    value = arg["default"]
            self._set_field_value(key, value)

        self.blockSignals(False)
        self.command_changed.emit()
        return had_passwords

    def reset_to_defaults(self) -> None:
        """Reset all fields to their schema defaults."""
        self.blockSignals(True)

        if self.sub_combo is not None:
            self.sub_combo.setCurrentIndex(0)

        self.extra_flags_group.setChecked(False)
        self.extra_flags_edit.clear()

        if self.elevation_check is not None:
            self.elevation_check.setChecked(self.data.get("elevated") == "always")

        for key, field in self.fields.items():
            arg = field["arg"]
            default = arg["default"]
            t = arg["type"]
            if t == "boolean":
                self._set_field_value(key, True if default is True else None)
            else:
                self._set_field_value(key, default)

        self.blockSignals(False)
        self.command_changed.emit()

    def _set_field_value(self, key: tuple, value) -> None:
        """Set a widget's value. None resets to empty/default."""
        field = self.fields.get(key)
        if not field:
            return
        w = field["widget"]
        arg = field["arg"]
        t = arg["type"]

        # Fallback widget — treat as plain string regardless of declared type
        if getattr(w, "_is_fallback", False):
            w.setText(str(value) if value is not None else "")
            return

        if t == "boolean":
            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            if field["repeat_spin"] and isinstance(value, int) and value > 1:
                field["repeat_spin"].setValue(value)
            elif field["repeat_spin"]:
                field["repeat_spin"].setValue(1)

        elif t == "string":
            if isinstance(w, QComboBox):
                w.setCurrentText(str(value) if value is not None else "")
            else:
                w.setText(str(value) if value is not None else "")

        elif t == "password":
            w._line_edit.setText(str(value) if value is not None else "")

        elif t == "text":
            w.setPlainText(str(value) if value is not None else "")

        elif t == "integer":
            if value is not None:
                try:
                    numeric = int(value)
                except (ValueError, TypeError):
                    return
                schema_min = arg.get("min")
                schema_max = arg.get("max")
                out_of_range = (
                    (schema_min is not None and numeric < schema_min)
                    or (schema_max is not None and numeric > schema_max)
                )
                w.setValue(numeric)
                if out_of_range:
                    clamped = w.value()
                    lo = schema_min if schema_min is not None else "*"
                    hi = schema_max if schema_max is not None else "*"
                    print(
                        f"[preset] value out of range for {arg['flag']}: "
                        f"preset={numeric} clamped to {clamped} "
                        f"(schema range: {lo}..{hi})",
                        file=sys.stderr,
                    )
            elif arg["default"] is not None:
                w.setValue(int(arg["default"]))
            else:
                w.setValue(w.minimum())

        elif t == "float":
            if value is not None:
                try:
                    numeric = float(value)
                except (ValueError, TypeError):
                    return
                if math.isnan(numeric) or math.isinf(numeric):
                    w.setValue(numeric)
                    clamped = w.value()
                    print(
                        f"[preset] non-finite value for {arg['flag']}: "
                        f"preset={numeric} coerced to {clamped}",
                        file=sys.stderr,
                    )
                else:
                    schema_min = arg.get("min")
                    schema_max = arg.get("max")
                    out_of_range = (
                        (schema_min is not None and numeric < schema_min)
                        or (schema_max is not None and numeric > schema_max)
                    )
                    w.setValue(numeric)
                    if out_of_range:
                        clamped = w.value()
                        lo = schema_min if schema_min is not None else "*"
                        hi = schema_max if schema_max is not None else "*"
                        print(
                            f"[preset] value out of range for {arg['flag']}: "
                            f"preset={numeric} clamped to {clamped} "
                            f"(schema range: {lo}..{hi})",
                            file=sys.stderr,
                        )
            elif arg["default"] is not None:
                w.setValue(float(arg["default"]))
            else:
                w.setValue(w.minimum())

        elif t == "enum":
            if value is not None:
                idx = w.findData(str(value))
                if idx >= 0:
                    w.setCurrentIndex(idx)
            else:
                w.setCurrentIndex(0)

        elif t == "multi_enum":
            selected = value if isinstance(value, list) else []
            for i in range(w.count()):
                item = w.item(i)
                if item.text() in selected:
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item.setCheckState(Qt.CheckState.Unchecked)

        elif t in ("file", "directory"):
            w._line_edit.setText(str(value) if value is not None else "")

    # ------------------------------------------------------------------
    # Command assembly
    # ------------------------------------------------------------------

    def is_elevation_checked(self) -> bool:
        """Return True if the elevation checkbox exists and is checked."""
        if self.elevation_check is not None:
            return self.elevation_check.isChecked()
        return _check_already_elevated() and self.data.get("elevated") in ("optional", "always")

    def build_command(self) -> tuple[list[str], str]:
        """Build the CLI command. Returns (cmd_list, display_string)."""
        cmd = [self.data["binary"]]
        positional = []

        sub = self.get_current_subcommand()

        # Process global args before inserting subcommand name
        self._assemble_args(self.data["arguments"], self.GLOBAL, cmd, positional)

        # Insert subcommand name and its args
        if sub:
            sub_data = next(s for s in self.data["subcommands"] if s["name"] == sub)
            cmd.extend(sub.split())
            self._assemble_args(sub_data["arguments"], sub, cmd, positional)

        # Positional args at end
        cmd.extend(positional)

        # Extra flags at very end
        cmd.extend(self.get_extra_flags())

        # Build display string
        display = _format_display(cmd)

        return cmd, display

    def _mask_passwords_for_display(self, cmd: list[str]) -> list[str]:
        """Return a copy of cmd with password-type values replaced by ********."""
        # Collect password flags and their separators for active scopes
        pw_flags = {}
        sub = self.get_current_subcommand()
        scopes = {self.GLOBAL}
        if sub:
            scopes.add(sub)
        for key, field in self.fields.items():
            scope, flag = key
            if scope not in scopes:
                continue
            if field["arg"]["type"] != "password":
                continue
            if self.get_field_value(key) is None:
                continue
            pw_flags[flag] = field["arg"].get("separator", "space")

        if not pw_flags:
            return list(cmd)

        # Build set of ALL flags in the schema (for prefix-collision checks)
        all_flags = set()
        for arg in self.data["arguments"]:
            all_flags.add(arg["flag"])
        for sub_data in (self.data["subcommands"] or []):
            for arg in sub_data["arguments"]:
                all_flags.add(arg["flag"])

        masked = []
        i = 0
        while i < len(cmd):
            token = cmd[i]
            matched = False
            if token in pw_flags and pw_flags[token] == "space":
                # Space separator: flag is one token, value is the next
                masked.append(token)
                if i + 1 < len(cmd):
                    i += 1
                    masked.append("********")
                matched = True
            if not matched:
                for flag, sep in pw_flags.items():
                    if sep == "equals" and token.startswith(f"{flag}="):
                        masked.append(f"{flag}=********")
                        matched = True
                        break
                    if sep == "none" and token.startswith(flag) and len(token) > len(flag):
                        # Check if a longer non-password flag also matches
                        longer_match = any(
                            other != flag
                            and token.startswith(other)
                            and len(other) > len(flag)
                            for other in all_flags
                        )
                        if longer_match:
                            continue
                        masked.append(f"{flag}********")
                        matched = True
                        break
            if not matched:
                masked.append(token)
            i += 1
        return masked

    def _assemble_args(self, args: list, scope: str, cmd: list, positional: list) -> None:
        """Append active flag tokens to cmd and active positional tokens to positional."""
        for arg in args:
            flag = arg["flag"]
            key = self._field_key(scope, flag)
            value = self.get_field_value(key)
            if value is None:
                continue

            t = arg["type"]
            sep = arg["separator"]

            if arg["positional"]:
                if t == "multi_enum":
                    positional.extend(value)
                else:
                    positional.append(str(value))
                continue

            if t == "boolean":
                count = value if isinstance(value, int) else 1
                for _ in range(count):
                    cmd.append(flag)

            elif t == "multi_enum":
                joined = ",".join(value)
                if sep == "space":
                    cmd.extend([flag, joined])
                elif sep == "equals":
                    cmd.append(f"{flag}={joined}")
                else:
                    cmd.append(f"{flag}{joined}")

            else:
                sv = str(value)
                if sep == "space":
                    cmd.extend([flag, sv])
                elif sep == "equals":
                    cmd.append(f"{flag}={sv}")
                else:
                    cmd.append(f"{flag}{sv}")

    def update_theme(self) -> None:
        """Update theme-sensitive widget styles (required labels, validation borders)."""
        req_color = _required_color()
        warn_color = DARK_COLORS["required"] if _dark_mode else "red"
        dep_color = DARK_COLORS["warning_text"] if _dark_mode else "#856404"

        for key, field in self.fields.items():
            arg = field["arg"]
            label = field["label"]

            # Rebuild label text only if it has any theme-sensitive decoration
            if arg["required"] or arg.get("dangerous") or arg.get("deprecated"):
                label_text = arg["name"]
                if arg["required"]:
                    label_text = f"<b>{label_text} <span style='color:{req_color};'>*</span></b>"
                if arg.get("dangerous"):
                    label_text = f"<span style='color:{warn_color};'>⚠</span> {label_text}"
                if arg.get("deprecated"):
                    label_text = f"<s>{label_text}</s> <span style='color:{dep_color};'>(deprecated)</span>"
                label.setText(label_text)

            w = field["widget"]
            if w.styleSheet() and "border" in w.styleSheet():
                w.setStyleSheet(_invalid_style())
            if hasattr(w, "_line_edit") and w._line_edit.styleSheet() and "border" in w._line_edit.styleSheet():
                w._line_edit.setStyleSheet(_invalid_style())
        # Header separator
        sep_color = DARK_COLORS["border"] if _dark_mode else "#999999"
        self._header_sep.setStyleSheet(
            f"QFrame {{ color: {sep_color}; max-height: 1px; margin: 4px 0 2px 0; }}"
        )
        # Elevation note
        if hasattr(self, "_elevation_note"):
            note_color = DARK_COLORS["text_dim"] if _dark_mode else "gray"
            self._elevation_note.setStyleSheet(f"color: {note_color}; font-style: italic;")
        # Search no-match label
        err_color = DARK_COLORS["error"] if _dark_mode else "red"
        self._search_no_match_label.setStyleSheet(f"color: {err_color}; font-style: italic; margin-left: 4px;")

    def validate_required(self) -> list[tuple]:
        """Check required fields. Returns list of field keys that are missing values."""
        missing = []
        # Determine active scopes
        scopes = [(self.GLOBAL, self.data["arguments"])]
        sub = self.get_current_subcommand()
        if sub:
            sub_data = next(s for s in self.data["subcommands"] if s["name"] == sub)
            scopes.append((sub, sub_data["arguments"]))

        for scope, args in scopes:
            for arg in args:
                if not arg["required"]:
                    continue
                key = self._field_key(scope, arg["flag"])
                value = self.get_field_value(key)
                field = self.fields.get(key)
                if not field:
                    continue
                w = field["widget"]
                if value is None:
                    missing.append(key)
                    if hasattr(w, '_line_edit'):
                        w._line_edit.setStyleSheet(_invalid_style())
                    elif hasattr(w, 'setStyleSheet'):
                        w.setStyleSheet(_invalid_style())
                else:
                    if key not in self.validators:
                        if hasattr(w, '_line_edit'):
                            w._line_edit.setStyleSheet("")
                        elif hasattr(w, 'setStyleSheet'):
                            w.setStyleSheet("")

        return missing


def _quote_token(token: str) -> str:
    """Shell-quote a token for display if it contains whitespace.

    Uses POSIX single-quote escaping (close-escape-reopen) when the
    token contains both whitespace and a single quote.
    """
    if " " in token or "\t" in token or "\n" in token or "\r" in token:
        if "'" not in token:
            return f"'{token}'"
        escaped = token.replace("'", "'\\''")
        return f"'{escaped}'"
    return token


def _format_display(cmd: list[str]) -> str:
    """Format a command list as a human-readable display string."""
    return " ".join(_quote_token(token) for token in cmd)


def _format_bash(cmd: list[str]) -> str:
    """Format a command list as a Bash command string."""
    if not cmd:
        return ""
    return shlex.join(cmd)


_SAFE_TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=+,@%-]+$')


def _format_powershell(cmd: list[str]) -> str:
    """Format a command list as a PowerShell command string.

    Uses a whitelist to decide safe tokens. Literal newlines and carriage
    returns are replaced with spaces before quoting, because PowerShell
    single-quoted strings cannot contain literal newlines.
    """
    if not cmd:
        return ""
    parts = []
    for i, token in enumerate(cmd):
        # Replace newlines — PS single-quoted strings cannot span lines
        token = token.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
        if _SAFE_TOKEN_RE.match(token):
            parts.append(token)
        elif i == 0:
            # Binary needs & prefix and single-quoting
            escaped = token.replace("'", "''")
            parts.append(f"& '{escaped}'")
        else:
            escaped = token.replace("'", "''")
            parts.append(f"'{escaped}'")
    return " ".join(parts)


def _format_cmd(cmd: list[str]) -> str:
    """Format a command list as a Windows CMD command string.

    Uses a whitelist to decide safe tokens. Literal newlines and carriage
    returns are replaced with spaces before quoting, because CMD
    double-quoted strings cannot contain literal newlines.
    """
    if not cmd:
        return ""
    parts = []
    for token in cmd:
        # Replace newlines — CMD double-quoted strings cannot span lines
        token = token.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
        if _SAFE_TOKEN_RE.match(token):
            parts.append(token)
        else:
            escaped = token.replace('"', '""')
            parts.append(f'"{escaped}"')
    return " ".join(parts)


_COPY_FORMATS = {
    "bash": (_format_bash, "Bash"),
    "powershell": (_format_powershell, "PowerShell"),
    "cmd": (_format_cmd, "CMD"),
    "generic": (_format_display, "Command"),
}


def _colored_preview_html(cmd: list[str], extra_count: int, subcommand: str | None = None) -> str:
    """Build an HTML string with syntax-colored command tokens.

    Args:
        cmd: The full command list from build_command().
        extra_count: Number of tokens at the end that are extra flags.
        subcommand: The active subcommand name, if any, for distinct coloring.
    """
    colors = DARK_PREVIEW if _dark_mode else LIGHT_PREVIEW
    parts = []
    core_len = len(cmd) - extra_count

    def _span(text: str, color: str, bold: bool = False, italic: bool = False) -> str:
        style = f"color:{color};"
        if bold:
            style += "font-weight:bold;"
        if italic:
            style += "font-style:italic;"
        return f"<span style='{style}'>{html.escape(text)}</span>"

    sub_parts = subcommand.split() if subcommand else []
    sub_idx = 0

    i = 0
    while i < len(cmd):
        token = cmd[i]
        display_token = _quote_token(token)

        if i == 0:
            # Binary
            parts.append(_span(display_token, colors["binary"], bold=True))
        elif i >= core_len:
            # Extra flags section
            parts.append(_span(display_token, colors["extra"], italic=True))
        elif sub_idx < len(sub_parts) and token == sub_parts[sub_idx]:
            # Subcommand part
            parts.append(_span(display_token, colors["subcommand"], bold=True))
            sub_idx += 1
        elif token.startswith("-"):
            # Flag — check for = separator (e.g., --flag=value)
            if "=" in token:
                flag_part, _, val_part = token.partition("=")
                display_flag = _quote_token(flag_part)
                display_val = _quote_token(val_part)
                parts.append(
                    _span(display_flag + "=", colors["flag"])
                    + _span(display_val, colors["value"])
                )
            else:
                parts.append(_span(display_token, colors["flag"]))
                # Next token is the value if it doesn't start with - (or is a negative number) and exists
                if i + 1 < core_len and not (sub_idx < len(sub_parts) and cmd[i + 1] == sub_parts[sub_idx]):
                    next_tok = cmd[i + 1]
                    is_value = not next_tok.startswith("-") or (
                        len(next_tok) > 1 and next_tok[1:].replace(".", "", 1).isdigit()
                    )
                    if is_value:
                        i += 1
                        val_display = _quote_token(cmd[i])
                        parts.append(_span(val_display, colors["value"]))
        else:
            # Positional or unrecognized value
            parts.append(_span(display_token, colors["value"]))

        i += 1

    return " ".join(parts)


def _monospace_font() -> QFont:
    """Return a Consolas/monospace QFont for use in command preview and output panels."""
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


def _is_portable_mode() -> bool:
    """True if portable.txt or scaffold.ini is next to scaffold.py.

    Portable mode forces source-mode behavior even if the script is in a
    site-packages-like location, so that fully-isolated installs (e.g. on
    a USB stick) work correctly.
    """
    script_dir = Path(__file__).parent
    return (script_dir / "portable.txt").exists() or (script_dir / "scaffold.ini").exists()


def _is_installed_mode() -> bool:
    """True if scaffold is running from an installed package.

    Detected by checking whether scaffold.py lives under site-packages
    or dist-packages. Portable mode overrides this — if portable.txt is
    present, we always treat as source mode regardless of script
    location.
    """
    if _is_portable_mode():
        return False
    parts = Path(__file__).resolve().parts
    return "site-packages" in parts or "dist-packages" in parts


def _bundled_root() -> Path:
    """Return the directory holding bundled files (tools/, default_presets/, prompts).

    Source / portable mode: scaffold.py's directory has scaffold_data/ as
    a sibling.

    Installed mode: scaffold_data is a sibling Python package, resolved
    via importlib.resources.

    If installed-mode resolution fails for any reason, falls back to the
    source-mode path. This makes the function safe to call during early
    startup before mode detection is fully reliable.
    """
    if _is_installed_mode():
        try:
            import importlib.resources as ir
            return Path(str(ir.files("scaffold_data")))
        except (ModuleNotFoundError, FileNotFoundError, TypeError):
            pass  # fall through
    return Path(__file__).parent / "scaffold_data"


def _user_data_root() -> Path:
    """Return the writable user-data directory (presets/, cascades/, user tools/).

    Source / portable mode: scaffold.py's parent directory (today's
    behavior).

    Installed mode: QStandardPaths.AppDataLocation/Scaffold/ — platform
    standard application data location:
      Linux:   ~/.local/share/Scaffold/
      macOS:   ~/Library/Application Support/Scaffold/
      Windows: %APPDATA%/Scaffold/

    The directory is created if missing.
    """
    if _is_installed_mode():
        loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if loc:
            d = Path(loc)
            # AppDataLocation only auto-appends QCoreApplication.applicationName()
            # when it has been set; scaffold doesn't set it at startup, so on a
            # fresh process the path is the platform root (e.g. %APPDATA%) with
            # no app suffix. Append "Scaffold" ourselves unless it's already there.
            if d.name != "Scaffold":
                d = d / "Scaffold"
            d.mkdir(parents=True, exist_ok=True)
            return d
    return Path(__file__).parent


def _app_root() -> Path:
    """Return the user-data root (alias for _user_data_root).

    Retained as a back-compat shim for call sites that resolve user-saved
    file paths (cascade tools, cascade presets, user tool delete dialog).
    For bundled read-only assets use _bundled_root() instead.
    """
    return _user_data_root()


def _resolve_app_relative(rel_path: str) -> Path | None:
    """Resolve a cascade-relative path against user-data first, then bundled.

    Returns the first existing absolute Path, or None if neither exists.
    Absolute paths are returned as-is without lookup.
    """
    p = Path(rel_path)
    if p.is_absolute():
        return p if p.exists() else None
    user = _user_data_root() / rel_path
    if user.exists():
        return user
    bundled = _bundled_root() / rel_path
    if bundled.exists():
        return bundled
    return None


def _create_settings() -> QSettings:
    """Return a QSettings using local INI file (portable) or system registry (default).

    If portable.txt or scaffold.ini exists next to this script, settings are stored
    in scaffold.ini (INI format) for fully isolated portable operation. Even in
    installed mode we use native QSettings — settings are OS-managed, not stored
    alongside user-data files.
    """
    if _is_portable_mode():
        script_dir = Path(__file__).parent
        return QSettings(str(script_dir / "scaffold.ini"), QSettings.Format.IniFormat)
    return QSettings("Scaffold", "Scaffold")


def _tools_dir() -> Path:
    """Return the writable user tools/ directory, creating it if needed.

    The picker reads from BOTH this directory and _bundled_tools_dir().
    User-added schemas live here.
    """
    d = _user_data_root() / "tools"
    if d.exists() and not d.is_dir():
        raise RuntimeError(f"Expected a directory but found a file at: {d}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundled_tools_dir() -> Path:
    """Return the read-only bundled tools/ directory."""
    return _bundled_root() / "tools"


def _presets_dir(tool_name: str) -> Path:
    """Return the presets/{tool_name}/ directory, creating it if needed.

    On first access for a tool, copies any bundled presets from
    {bundled}/default_presets/{tool_name}/ into the user's preset directory
    (without overwriting existing files).
    """
    d = _user_data_root() / "presets" / tool_name
    if d.exists() and not d.is_dir():
        raise RuntimeError(f"Expected a directory but found a file at: {d}")
    d.mkdir(parents=True, exist_ok=True)
    # Seed from bundled defaults (skip files the user already has)
    defaults = _bundled_root() / "default_presets" / tool_name
    if defaults.is_dir():
        for src in defaults.glob("*.json"):
            dest = d / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
    return d


def _cascades_dir() -> Path:
    """Return the cascades/ directory in user data, creating it if needed."""
    d = _user_data_root() / "cascades"
    if d.exists() and not d.is_dir():
        raise RuntimeError(f"Expected a directory but found a file at: {d}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cascade_path_is_safe(base: Path, value: str) -> bool:
    """Return True if a cascade step's path value is safe to join with base.

    Absolute paths pass through unchanged (matches _load_tool_path's general
    policy — tool schemas and presets can live anywhere on disk). Relative
    paths are resolved against base and rejected if they escape it via '..'
    traversal. Empty/None values are treated as safe (caller handles the
    empty case separately).
    """
    if not value:
        return True
    try:
        p = Path(value)
    except (TypeError, ValueError):
        return False
    if p.is_absolute():
        return True
    try:
        resolved = (base / value).resolve()
        base_resolved = base.resolve()
    except (OSError, ValueError):
        return False
    try:
        return resolved.is_relative_to(base_resolved)
    except AttributeError:
        # Python <3.9 fallback — scaffold targets 3.9+ so this is defensive
        try:
            resolved.relative_to(base_resolved)
            return True
        except ValueError:
            return False


def _check_cascade_dependencies(cascade_data: dict) -> tuple[list[str], list[str]]:
    """Return (missing_tools, missing_presets) for a cascade's referenced files.

    Paths are resolved via _resolve_app_relative (user dir first, then
    bundled), matching _import_cascade_data's resolver. Entries are the
    relative-path strings as stored in the cascade (same form the user
    sees). Empty/None preset or tool fields are treated as absent, not
    missing. The safety check is anchored at the user-data root so
    cascade paths cannot escape via '..'.
    """
    user_base = _user_data_root()
    missing_tools: list[str] = []
    missing_presets: list[str] = []
    for step in cascade_data.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        if tool:
            if not _cascade_path_is_safe(user_base, tool):
                missing_tools.append(tool)
            elif _resolve_app_relative(tool) is None:
                missing_tools.append(tool)
        preset = step.get("preset")
        if preset:
            if not _cascade_path_is_safe(user_base, preset):
                missing_presets.append(preset)
            elif _resolve_app_relative(preset) is None:
                missing_presets.append(preset)
    return missing_tools, missing_presets


def _prompt_cascade_missing_deps(parent, missing_tools: list[str], missing_presets: list[str]) -> bool:
    """Warn the user about missing cascade dependencies. Returns True to proceed.

    If both lists are empty, returns True without showing a dialog so that
    cascades with full dependency coverage import/load silently.
    """
    if not missing_tools and not missing_presets:
        return True
    parts: list[str] = []
    if missing_tools:
        parts.append("Missing tool schemas:")
        parts.extend(f"  \u2022 {t}" for t in missing_tools)
    if missing_presets:
        if parts:
            parts.append("")
        parts.append("Missing presets:")
        parts.extend(f"  \u2022 {p}" for p in missing_presets)
    parts.append("")
    parts.append("Some steps will fail when you run this cascade. Continue anyway?")
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Missing cascade dependencies")
    box.setText("\n".join(parts))
    continue_btn = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
    cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel_btn)
    box.exec()
    return box.clickedButton() is continue_btn


def _get_custom_paths() -> list[str]:
    """Read custom PATH directories from QSettings.

    Returns a list of existing directory paths (filters out missing/empty entries).
    The value is stored as a JSON array string under the key "custom_paths".
    """
    s = _create_settings()
    raw = s.value("custom_paths", "")
    if not raw:
        return []
    try:
        paths = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(paths, list):
        return []
    return [p for p in paths if p and isinstance(p, str) and Path(p).is_dir()]


def _set_custom_paths(paths: list[str]) -> None:
    """Write custom PATH directories to QSettings as a JSON array string.

    Paths are normalized to absolute form and entries containing the OS
    path separator are dropped.
    """
    s = _create_settings()
    cleaned = [p for p in paths if isinstance(p, str) and p and os.pathsep not in p]
    cleaned = [os.path.abspath(p) for p in cleaned]
    cleaned = [p for p in cleaned if p]
    s.setValue("custom_paths", json.dumps(cleaned))


def _extended_path() -> str | None:
    """Return a PATH string with custom directories prepended, or None if none configured."""
    custom = _get_custom_paths()
    if not custom:
        return None
    system_path = os.environ.get("PATH", "")
    return os.pathsep.join(custom) + os.pathsep + system_path


def _binary_in_path(binary: str) -> bool:
    """Check if binary is found in PATH (including custom directories)."""
    search_path = _extended_path()
    return shutil.which(binary, path=search_path) is not None


def _fit_last_column(widget) -> None:
    """Make the last column fill remaining viewport space.

    Works with both QTableWidget (horizontalHeader) and QTreeWidget (header).
    """
    header = widget.horizontalHeader() if hasattr(widget, 'horizontalHeader') else widget.header()
    last_col = widget.columnCount() - 1
    used = sum(header.sectionSize(i) for i in range(last_col))
    remaining = widget.viewport().width() - used
    header.resizeSection(last_col, max(remaining, 50))


def _clamp_for_last_column(widget, logical_index: int, old_size: int, new_size: int) -> None:
    """After a column resize, adjust the last column to fill remaining space.

    If the resized column took too much space, clamp it so the last column
    keeps at least 50px.  Works with both QTableWidget and QTreeWidget.
    """
    header = widget.horizontalHeader() if hasattr(widget, 'horizontalHeader') else widget.header()
    last_col = widget.columnCount() - 1
    if logical_index == last_col:
        return
    used = sum(header.sectionSize(i) for i in range(last_col))
    remaining = widget.viewport().width() - used
    if remaining < 50:
        overflow = 50 - remaining
        header.blockSignals(True)
        header.resizeSection(logical_index, max(new_size - overflow, 50))
        used = sum(header.sectionSize(i) for i in range(last_col))
        remaining = widget.viewport().width() - used
        header.resizeSection(last_col, max(remaining, 50))
        header.blockSignals(False)
    else:
        header.blockSignals(True)
        header.resizeSection(last_col, remaining)
        header.blockSignals(False)


def _wire_last_column(
    owner,
    table,
    *,
    extra=None,
) -> None:
    """Wire a table's sectionResized signal to clamp the last column.

    Parameters:
        owner: the widget/dialog that owns `table`; used only to keep the
               connection alive for its lifetime.
        table: the QTableWidget / QTreeWidget / QTreeView whose header's
               sectionResized signal should be connected.
        extra: optional callback of shape (idx, old, new) -> None called
               AFTER _clamp_for_last_column on every resize. Used for
               call sites that need additional per-column logic (e.g.,
               minimum/maximum bounds on a specific column).
    """
    if hasattr(table, "horizontalHeader"):
        header = table.horizontalHeader()
    else:
        header = table.header()

    def _handler(idx: int, old: int, new: int) -> None:
        _clamp_for_last_column(table, idx, old, new)
        if extra is not None:
            extra(idx, old, new)

    header.sectionResized.connect(_handler)


# ---------------------------------------------------------------------------
# Tool picker
# ---------------------------------------------------------------------------

class ToolPicker(QWidget):
    """Displays available tools and lets the user pick one."""

    tool_selected = Signal(str)  # emits the file path
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        header = QLabel("Scaffold — Select a Tool")
        header.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(header)

        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter tools...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        self.search_bar.installEventFilter(self)
        self._filter_cycle_index = -1
        layout.addWidget(self.search_bar)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["", "Tool", "Description", "Path"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setCascadingSectionResizes(True)
        self.table.horizontalHeader().setMinimumSectionSize(50)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Interactive
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        _wire_last_column(self, self.table)
        _orig_resize = self.table.resizeEvent
        self.table.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self.table))
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table, 1)

        tools_abs = str(_tools_dir().resolve())
        self.empty_label = QLabel(
            f"No tools found. Place JSON schema files in:\n{tools_abs}\n"
            f"or use File \u2192 Load to open one."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: gray; padding: 40px;")
        layout.addWidget(self.empty_label)

        btn_bar = QHBoxLayout()
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(self._on_open)
        btn_bar.addWidget(self.open_btn)

        load_btn = QPushButton("Load from File...")
        load_btn.clicked.connect(self._on_load_file)
        btn_bar.addWidget(load_btn)

        btn_bar.addStretch()

        self.back_btn = QPushButton("Back")
        self.back_btn.setEnabled(False)
        self.back_btn.clicked.connect(self.back_requested.emit)
        btn_bar.addWidget(self.back_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete_tool)
        btn_bar.addWidget(self.delete_btn)

        layout.addLayout(btn_bar)

        self._entries = []  # list of (path, data_or_none, error_or_none, binary_available)
        self._row_map = []  # parallel to table rows: int (index into _entries) or None (header)
        self._collapsed_folders: set[str] = set()
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        self.table.cellClicked.connect(self._on_cell_clicked)
        QWidget.setTabOrder(self.search_bar, self.table)
        self.scan()

    def _entry_folder(self, path: str, is_bundled: bool) -> str:
        """Return the relative folder name for a tool path. '' for root tools."""
        root = _bundled_tools_dir() if is_bundled else _tools_dir()
        try:
            folder = Path(path).parent.relative_to(root).as_posix()
        except ValueError:
            folder = ""
        return "" if folder == "." else folder

    def _entry_relpath(self, path: str, is_bundled: bool) -> str:
        """Return the relative-to-tools-root path for display in the path column."""
        root = _bundled_tools_dir() if is_bundled else _tools_dir()
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            return Path(path).name

    def scan(self):
        """Scan user and bundled tools directories and populate the table.

        User tools take precedence — if the same filename appears in both,
        the bundled copy is hidden (the user's override wins).
        """
        self._entries.clear()
        user_path = _tools_dir()
        bundled_path = _bundled_tools_dir()

        # Scan user tools first, record their filenames so bundled duplicates are skipped
        user_files: list[Path] = sorted(user_path.rglob("*.json"))
        user_names = {p.name for p in user_files}

        for json_file in user_files:
            self._add_scan_entry(json_file, is_bundled=False)

        if bundled_path.exists():
            for json_file in sorted(bundled_path.rglob("*.json")):
                if json_file.name in user_names:
                    continue
                self._add_scan_entry(json_file, is_bundled=True)

        # Sort: folder groups first (alphabetically), then root tools.
        # Within each group: available first, then unavailable, then invalid — alphabetical within each.
        def _sort_key(entry):
            path, data, error, available, is_bundled = entry
            folder = self._entry_folder(path, is_bundled)
            if error:
                priority = 2  # invalid last
            elif available:
                priority = 0  # available first
            else:
                priority = 1  # unavailable middle
            name = data["tool"].lower() if data else Path(path).name.lower()
            return (0 if folder else 1, folder, priority, name)

        self._entries.sort(key=_sort_key)
        self._populate_table()

    def _add_scan_entry(self, json_file: Path, *, is_bundled: bool) -> None:
        """Validate and append a single tool file to self._entries."""
        try:
            data = load_tool(str(json_file))
            errors = validate_tool(data)
            if errors:
                self._entries.append((str(json_file), None, "; ".join(errors), False, is_bundled))
            else:
                data = normalize_tool(data)
                available = _binary_in_path(data["binary"])
                self._entries.append((str(json_file), data, None, available, is_bundled))
        except RuntimeError as e:
            self._entries.append((str(json_file), None, str(e), False, is_bundled))

    def _populate_table(self) -> None:
        """Render self._entries into the tool-picker table."""
        self._row_map = []  # parallel to table rows: int (index into _entries) or None (header)
        self._row_folder: list[str] = []  # parallel: visual folder name for each row (incl. headers)

        # Group entries by folder for header insertion
        folder_groups: list[tuple[str, list[int]]] = []  # (folder, [entry indices])
        current_folder: str | None = None
        for idx, (path, _, _, _, is_bundled) in enumerate(self._entries):
            folder = self._entry_folder(path, is_bundled)
            if folder != current_folder:
                folder_groups.append((folder, []))
                current_folder = folder
            folder_groups[-1][1].append(idx)

        # Prepend "Recently used" virtual group (duplicate-display, MRU order).
        # Stale paths and entries with no scanned match are dropped silently.
        recent_indices: list[int] = []
        raw_recent = _create_settings().value("picker/recent_tools", "")
        if raw_recent:
            try:
                paths = json.loads(raw_recent)
            except (json.JSONDecodeError, TypeError):
                paths = []
            if isinstance(paths, list):
                path_to_idx = {entry[0]: i for i, entry in enumerate(self._entries)}
                for p in paths:
                    if isinstance(p, str) and p in path_to_idx:
                        recent_indices.append(path_to_idx[p])
        if recent_indices:
            folder_groups.insert(0, ("Recently used", recent_indices))

        # Count total rows: tool rows + header rows (one per non-root folder group)
        total_rows = sum(len(indices) for _, indices in folder_groups)
        for folder, _indices in folder_groups:
            if folder:  # non-root folders get a header row
                total_rows += 1

        self.table.setRowCount(total_rows)
        table_row = 0

        header_bg = QColor(DARK_COLORS["widget"]) if _dark_mode else QColor("#e8e8e8")
        header_fg = QColor(DARK_COLORS["text"]) if _dark_mode else QColor("#333333")
        bold_font = QFont()
        bold_font.setBold(True)

        for folder, indices in folder_groups:
            # Insert header row for non-root folders
            if folder:
                for col in range(4):
                    if col == 1:
                        arrow = "\u25b6" if folder in self._collapsed_folders else "\u25bc"
                        h_item = QTableWidgetItem(f"{arrow} {folder}")
                    else:
                        h_item = QTableWidgetItem("")
                    h_item.setFlags(Qt.ItemFlag(0))  # non-selectable
                    h_item.setBackground(header_bg)
                    if col == 1:
                        h_item.setFont(bold_font)
                        h_item.setForeground(header_fg)
                    self.table.setItem(table_row, col, h_item)
                self._row_map.append(None)
                self._row_folder.append(folder)
                table_row += 1

            for entry_idx in indices:
                path, data, error, available, is_bundled = self._entries[entry_idx]
                fname = Path(path).name
                rel_path = self._entry_relpath(path, is_bundled)
                if is_bundled:
                    rel_path = f"{rel_path}  (bundled)"

                # Status column
                if error:
                    status_item = QTableWidgetItem("")
                elif available:
                    status_item = QTableWidgetItem("\u2714")  # checkmark
                    status_item.setForeground(QColor(COLOR_OK))
                    status_item.setToolTip(f"'{data['binary']}' found in PATH — ready to use")
                else:
                    status_item = QTableWidgetItem("\u2716")  # X mark
                    status_item.setForeground(QColor(COLOR_ERR))
                    status_item.setToolTip(
                        f"<p>'{html.escape(data['binary'])}' not found in PATH. "
                        "Install it or add its location to your PATH.</p>"
                    )
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                if data:
                    name_item = QTableWidgetItem(data["tool"])
                    desc_text = data.get("description", "")
                    desc_item = QTableWidgetItem(desc_text)
                    if desc_text:
                        desc_item.setToolTip(f"<p style='max-width:400px;'>{html.escape(desc_text)}</p>")
                else:
                    name_item = QTableWidgetItem(fname)
                    desc_item = QTableWidgetItem(f"[invalid] {error}")
                path_item = QTableWidgetItem(rel_path)

                if error:
                    for item in (status_item, name_item, desc_item, path_item):
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                        item.setToolTip(error)
                elif not available:
                    # Dim unavailable tools slightly but keep them selectable
                    for item in (name_item, desc_item, path_item):
                        item.setForeground(QColor(COLOR_DIM))

                self.table.setItem(table_row, 0, status_item)
                self.table.setItem(table_row, 1, name_item)
                self.table.setItem(table_row, 2, desc_item)
                self.table.setItem(table_row, 3, path_item)
                self._row_map.append(entry_idx)
                self._row_folder.append(folder)
                table_row += 1

        has_items = len(self._entries) > 0
        self.table.setVisible(has_items)
        self.empty_label.setVisible(not has_items)
        self.open_btn.setEnabled(False)
        self.search_bar.clear()
        self.search_bar.setFocus()
        self.table.resizeColumnToContents(1)
        _fit_last_column(self.table)

    def _update_header_colors(self) -> None:
        """Refresh folder-header row backgrounds for current theme."""
        if not hasattr(self, "_row_map"):
            return
        header_bg = QColor(DARK_COLORS["widget"]) if _dark_mode else QColor("#e8e8e8")
        header_fg = QColor(DARK_COLORS["text"]) if _dark_mode else QColor("#333333")
        for r, entry in enumerate(self._row_map):
            if entry is None:  # header row
                for col in range(self.table.columnCount()):
                    item = self.table.item(r, col)
                    if item is not None:
                        item.setBackground(header_bg)
                        if col == 1:
                            item.setForeground(header_fg)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.PaletteChange:
            self._update_header_colors()
        super().changeEvent(event)

    def _on_filter(self, text: str) -> None:
        """Hide table rows that don't match the search text or are in collapsed folders."""
        query = text.strip().lower()
        # First pass: determine visibility for tool rows; track header rows
        header_rows: list[int] = []  # table rows that are headers
        header_has_visible: dict[int, bool] = {}  # header_row -> any tool visible
        current_header: int | None = None
        for row in range(self.table.rowCount()):
            entry_idx = self._row_map[row] if row < len(self._row_map) else None
            if entry_idx is None:
                # Header row — track it, decide visibility after scanning its tools
                header_rows.append(row)
                header_has_visible[row] = False
                current_header = row
                continue
            # Check search match
            matches_search = True
            if query:
                matches_search = False
                for col in (1, 2, 3):  # tool name, description, path
                    item = self.table.item(row, col)
                    if item and query in item.text().lower():
                        matches_search = True
                        break
            # Check collapse state — use the visual folder (set during _populate_table),
            # so a tool that appears under both "Recently used" and its real folder
            # is hidden/shown per-section, not per-entry.
            folder = self._row_folder[row] if row < len(self._row_folder) else ""
            is_collapsed = folder in self._collapsed_folders
            self.table.setRowHidden(row, not matches_search or is_collapsed)
            if matches_search and current_header is not None and folder:
                header_has_visible[current_header] = True
        # Second pass: show/hide header rows based on whether any child tool matches search
        for h_row in header_rows:
            self.table.setRowHidden(h_row, not header_has_visible[h_row])
        self._filter_cycle_index = -1

    def _visible_tool_rows(self) -> list[int]:
        """Return table row indices of visible, selectable tool rows."""
        rows = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            if row < len(self._row_map) and self._row_map[row] is None:
                continue  # header row
            rows.append(row)
        return rows

    def _filter_next(self) -> None:
        """Select the next visible tool row (Enter in filter bar)."""
        visible = self._visible_tool_rows()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index + 1) % len(visible)
        row = visible[self._filter_cycle_index]
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    def _filter_prev(self) -> None:
        """Select the previous visible tool row (Shift+Enter in filter bar)."""
        visible = self._visible_tool_rows()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index - 1) % len(visible)
        row = visible[self._filter_cycle_index]
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    def eventFilter(self, obj, event) -> bool:
        """Catch Enter/Shift+Enter on filter bar for match cycling."""
        if obj is self.search_bar and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._filter_prev()
                else:
                    self._filter_next()
                return True
        return super().eventFilter(obj, event)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """Toggle folder collapse when a header row is clicked."""
        if row >= len(self._row_map) or self._row_map[row] is not None:
            return  # tool row or out of range
        item = self.table.item(row, 1)
        if not item:
            return
        text = item.text()
        for prefix in ("\u25bc ", "\u25b6 "):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if text in self._collapsed_folders:
            self._collapsed_folders.discard(text)
        else:
            self._collapsed_folders.add(text)
        self._apply_collapse()

    def _apply_collapse(self) -> None:
        """Update arrow indicators on header rows and reapply visibility."""
        for row in range(self.table.rowCount()):
            if row >= len(self._row_map) or self._row_map[row] is not None:
                continue
            item = self.table.item(row, 1)
            if not item:
                continue
            text = item.text()
            for prefix in ("\u25bc ", "\u25b6 "):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            arrow = "\u25b6" if text in self._collapsed_folders else "\u25bc"
            item.setText(f"{arrow} {text}")
        self._on_filter(self.search_bar.text())

    def keyPressEvent(self, event) -> None:
        """Handle Enter key to open the selected tool."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_open()
        else:
            super().keyPressEvent(event)

    def _on_selection(self) -> None:
        """Enable or disable Open/Delete buttons based on whether a valid tool row is selected."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.open_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            return
        row = rows[0].row()
        entry_idx = self._row_map[row] if row < len(self._row_map) else None
        if entry_idx is None:
            self.open_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            return
        _, data, _, _, is_bundled = self._entries[entry_idx]
        valid = data is not None
        self.open_btn.setEnabled(valid)
        # Bundled tools are read-only — keep delete disabled so users get the right
        # affordance up-front. Clicking when somehow enabled still surfaces a status
        # message in _on_delete_tool as a backstop.
        self.delete_btn.setEnabled(valid and not is_bundled)

    def _on_double_click(self, index) -> None:
        """Emit tool_selected for the double-clicked row if the tool is valid."""
        row = index.row()
        entry_idx = self._row_map[row] if row < len(self._row_map) else None
        if entry_idx is None:
            return
        path, data, _, _, _ = self._entries[entry_idx]
        if data is not None:
            self.tool_selected.emit(path)

    def _on_open(self) -> None:
        """Emit tool_selected for the currently selected table row."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        entry_idx = self._row_map[row] if row < len(self._row_map) else None
        if entry_idx is None:
            return
        path, data, _, _, _ = self._entries[entry_idx]
        if data is not None:
            self.tool_selected.emit(path)

    def _on_delete_tool(self) -> None:
        """Delete the selected tool schema (and optionally its presets) after confirmation."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        entry_idx = self._row_map[row] if row < len(self._row_map) else None
        if entry_idx is None:
            return
        path_str, data, _, _, _ = self._entries[entry_idx]
        if data is None:
            return

        tool_name = data["tool"]
        tool_path = Path(path_str).resolve()
        tools_dir = _tools_dir().resolve()
        bundled_tools_dir = _bundled_tools_dir().resolve() if _bundled_tools_dir().exists() else None

        # Bundled tools are read-only — refuse delete and tell the user where to add custom schemas.
        if bundled_tools_dir is not None and bundled_tools_dir in tool_path.parents:
            mw = self.window()
            if hasattr(mw, "statusBar"):
                mw.statusBar().showMessage(
                    f"Bundled tools are read-only. Drop your own *.json into {tools_dir} to add custom schemas."
                )
            return

        # Safety: ensure the file is inside the user tools directory
        if tools_dir not in tool_path.parents:
            QMessageBox.warning(self, "Error", "File is not inside the tools directory.")
            return

        filename = tool_path.name
        presets_base = _user_data_root() / "presets"
        preset_dir = presets_base / tool_name
        preset_files = list(preset_dir.glob("*.json")) if preset_dir.is_dir() else []
        has_presets = len(preset_files) > 0

        if has_presets:
            msg = QMessageBox(self)
            msg.setWindowTitle("Delete Tool")
            msg.setText(
                f"Delete tool schema '{tool_name}'?\n\n"
                f"This will permanently remove {filename} from the tools directory.\n"
                f"This tool has {len(preset_files)} saved preset(s).\n\n"
                "Tip: Bundled files can be restored with:\n"
                f"  git checkout -- tools/{filename}\n"
                f"  git checkout -- presets/{tool_name}/"
            )
            btn_all = msg.addButton("Delete All", QMessageBox.ButtonRole.DestructiveRole)
            btn_schema = msg.addButton("Schema Only", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btn_all:
                delete_presets = True
            elif clicked == btn_schema:
                delete_presets = False
            else:
                return
        else:
            confirm = QMessageBox.question(
                self, "Delete Tool Schema",
                f"Delete schema '{tool_name}'?\n\n"
                f"The file {filename} will be permanently removed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            delete_presets = False

        # Delete the schema file
        try:
            tool_path.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Delete Failed", f"Could not delete schema:\n{e}")
            return

        # Delete presets if requested
        if delete_presets and preset_dir.is_dir():
            resolved_preset = preset_dir.resolve()
            if presets_base.resolve() in resolved_preset.parents:
                try:
                    shutil.rmtree(resolved_preset)
                except OSError as e:
                    QMessageBox.warning(
                        self, "Partial Delete",
                        f"Schema deleted but failed to remove presets:\n{e}",
                    )

        self.scan()

    def _on_load_file(self) -> None:
        """Open a file-picker dialog and emit tool_selected for the chosen JSON."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Tool JSON", str(_tools_dir()), "JSON Files (*.json)"
        )
        if path:
            self.tool_selected.emit(path)


# ---------------------------------------------------------------------------
# Preset picker dialog
# ---------------------------------------------------------------------------

class PresetPicker(QDialog):
    """Modal dialog for selecting a preset, with favorite-star support."""

    def __init__(self, tool_name: str, presets_dir: Path, mode: str, parent=None):
        super().__init__(parent)
        self.tool_name = tool_name
        self.presets_dir = presets_dir
        self.mode = mode  # "load", "delete", or "edit"
        self.selected_path: str | None = None
        self._presets: list[Path] = []  # ordered list matching table rows
        self._deleted_last = False

        titles = {"load": "Preset List", "delete": "Delete Preset", "edit": "Edit Preset"}
        self.setWindowTitle(f"{titles.get(mode, mode)} \u2014 {tool_name}")
        self.resize(600, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # Filter bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter presets...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        self.search_bar.installEventFilter(self)
        self._filter_cycle_index = -1
        layout.addWidget(self.search_bar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["\u2605", "Preset", "Description", "Last Modified"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setCascadingSectionResizes(True)
        self.table.horizontalHeader().setMinimumSectionSize(50)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Interactive
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        _wire_last_column(self, self.table)
        _orig_resize = self.table.resizeEvent
        self.table.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self.table))
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        layout.addWidget(self.table, 1)

        # Buttons — layout depends on mode
        btn_bar = QHBoxLayout()
        self.delete_btn = None  # only created in edit mode

        if mode == "edit":
            self.edit_desc_btn = QPushButton("Edit Description...")
            self.edit_desc_btn.setEnabled(False)
            self.edit_desc_btn.clicked.connect(self._on_edit_description)
            btn_bar.addWidget(self.edit_desc_btn)
            self.delete_btn = QPushButton("Delete")
            self.delete_btn.setEnabled(False)
            self.delete_btn.clicked.connect(self._on_delete)
            btn_bar.addWidget(self.delete_btn)
            btn_bar.addStretch()
            self.action_btn = None  # no load/accept action in edit mode
            self.back_btn = QPushButton("Back")
            self.back_btn.clicked.connect(self.reject)
            btn_bar.addWidget(self.back_btn)
        else:
            btn_bar.addStretch()
            action_label = "Load" if mode == "load" else "Delete"
            self.action_btn = QPushButton(action_label)
            self.action_btn.setEnabled(False)
            self.action_btn.clicked.connect(self._on_action)
            btn_bar.addWidget(self.action_btn)
            self.edit_desc_btn = QPushButton("Edit Description...")
            self.edit_desc_btn.setEnabled(False)
            self.edit_desc_btn.clicked.connect(self._on_edit_description)
            btn_bar.addWidget(self.edit_desc_btn)
            self.back_btn = QPushButton("Back")
            self.back_btn.clicked.connect(self.reject)
            btn_bar.addWidget(self.back_btn)

        layout.addLayout(btn_bar)

        # Load favorites and populate
        self._favorites = self._load_favorites()
        self._populate()
        QWidget.setTabOrder(self.search_bar, self.table)

    def _load_favorites(self) -> set[str]:
        """Read the favorites list for this tool from QSettings."""
        settings = _create_settings()
        raw = settings.value(f"favorites/{self.tool_name}", "[]")
        try:
            names = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(names, list):
                return set()
            return set(names)
        except (json.JSONDecodeError, TypeError):
            return set()

    def _save_favorites(self) -> None:
        """Write the favorites list for this tool to QSettings."""
        settings = _create_settings()
        settings.setValue(
            f"favorites/{self.tool_name}",
            json.dumps(sorted(self._favorites)),
        )

    def _populate(self) -> None:
        """Scan presets_dir and fill the table, cleaning stale favorites."""
        preset_files = sorted(self.presets_dir.glob("*.json"))

        # Clean stale favorites
        on_disk = {p.stem for p in preset_files}
        stale = self._favorites - on_disk
        if stale:
            self._favorites -= stale
            self._save_favorites()

        # Sort: favorites first, then alphabetical within each group
        def _sort_key(p: Path):
            is_fav = 0 if p.stem in self._favorites else 1
            return (is_fav, p.stem.lower())

        preset_files.sort(key=_sort_key)
        self._presets = preset_files

        self.table.setRowCount(len(preset_files))
        for row, p in enumerate(preset_files):
            name = p.stem
            is_fav = name in self._favorites

            # Column 0 — star
            star_item = QTableWidgetItem("\u2605" if is_fav else "\u2606")
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, star_item)

            # Column 1 — name
            self.table.setItem(row, 1, QTableWidgetItem(name))

            # Column 2 — description (read _description from JSON)
            desc = ""
            try:
                pdata = _read_json_file(p)
                desc = pdata.get("_description", "") or ""
            except (json.JSONDecodeError, OSError):
                pass
            desc_item = QTableWidgetItem(desc)
            if desc:
                desc_item.setToolTip(f"<p style='max-width:400px;'>{html.escape(desc)}</p>")
            self.table.setItem(row, 2, desc_item)

            # Column 3 — last modified
            try:
                mtime = p.stat().st_mtime
                dt = datetime.datetime.fromtimestamp(mtime)
                date_str = dt.strftime("%b %d, %Y %I:%M %p")
            except OSError:
                date_str = "Unknown"
            self.table.setItem(row, 3, QTableWidgetItem(date_str))

        self.table.resizeColumnToContents(1)
        _fit_last_column(self.table)

        if self.action_btn is not None:
            self.action_btn.setEnabled(False)
        self.edit_desc_btn.setEnabled(False)
        if self.delete_btn is not None:
            self.delete_btn.setEnabled(False)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """Toggle favorite star when column 0 is clicked."""
        if col != 0:
            return
        if row < 0 or row >= len(self._presets):
            return
        name = self._presets[row].stem
        if name in self._favorites:
            self._favorites.discard(name)
        else:
            self._favorites.add(name)
        self._save_favorites()

        # Remember selection, re-populate, try to re-select same preset
        self._populate()
        for r in range(len(self._presets)):
            if self._presets[r].stem == name:
                self.table.selectRow(r)
                break

    def _on_selection(self) -> None:
        """Enable/disable action buttons based on selection."""
        rows = self.table.selectionModel().selectedRows()
        has_selection = len(rows) > 0
        if self.action_btn is not None:
            self.action_btn.setEnabled(has_selection)
        self.edit_desc_btn.setEnabled(has_selection)
        if self.delete_btn is not None:
            self.delete_btn.setEnabled(has_selection)

    def _on_filter(self, text: str) -> None:
        """Hide rows that don't match the filter text (name or description)."""
        query = text.strip().lower()
        for row in range(self.table.rowCount()):
            if not query:
                self.table.setRowHidden(row, False)
                continue
            matches = False
            for col in (1, 2):  # name, description
                item = self.table.item(row, col)
                if item and query in item.text().lower():
                    matches = True
                    break
            self.table.setRowHidden(row, not matches)
        self._filter_cycle_index = -1

    def _visible_preset_rows(self) -> list[int]:
        """Return row indices of visible preset rows."""
        return [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]

    def _filter_next(self) -> None:
        """Select the next visible row (Enter in filter bar)."""
        visible = self._visible_preset_rows()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index + 1) % len(visible)
        row = visible[self._filter_cycle_index]
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    def _filter_prev(self) -> None:
        """Select the previous visible row (Shift+Enter in filter bar)."""
        visible = self._visible_preset_rows()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index - 1) % len(visible)
        row = visible[self._filter_cycle_index]
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))

    def eventFilter(self, obj, event) -> bool:
        """Catch Enter/Shift+Enter on filter bar for match cycling."""
        if obj is self.search_bar and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._filter_prev()
                else:
                    self._filter_next()
                return True
        return super().eventFilter(obj, event)

    def _on_double_click(self, index) -> None:
        """Accept the dialog on double-click (load/delete modes only)."""
        if self.mode == "edit":
            return
        row = index.row()
        if 0 <= row < len(self._presets):
            self.selected_path = str(self._presets[row])
            self.accept()

    def _on_action(self) -> None:
        """Accept the dialog with the selected preset path."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self._presets):
            self.selected_path = str(self._presets[row])
            self.accept()

    def _on_edit_description(self) -> None:
        """Edit the _description field of the selected preset."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._presets):
            return
        preset_path = self._presets[row]

        # Read current preset
        try:
            data = _read_user_json(preset_path)
        except RuntimeError as e:
            QMessageBox.warning(self, "Error", f"Cannot read preset file:\n{e}")
            return
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Error", f"Cannot read preset file:\n{e}")
            return

        current_desc = data.get("_description", "")
        new_desc, ok = QInputDialog.getText(
            self, "Edit Description", "Description:", text=current_desc,
        )
        if not ok:
            return

        # Update only _description and write back
        data["_description"] = new_desc.strip()
        try:
            _atomic_write_json(preset_path, data)
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Cannot write preset file:\n{e}")
            return

        # Update table cell in place
        desc_item = self.table.item(row, 2)
        if desc_item:
            desc_item.setText(new_desc.strip())
        titles = {"load": "Preset List", "delete": "Delete Preset", "edit": "Edit Preset"}
        self.setWindowTitle(
            f"{titles.get(self.mode, self.mode)}"
            f" \u2014 {self.tool_name} \u2014 Description updated"
        )

    def _on_delete(self) -> None:
        """Delete the selected preset (edit mode). Confirms, removes file and table row."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._presets):
            return
        preset_path = self._presets[row]
        name = preset_path.stem

        confirm = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete preset '{name}'?\n\n"
            "Tip: Bundled presets can be restored with:\n"
            f"  git checkout -- presets/{self.tool_name}/{name}.json",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            preset_path.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Cannot delete preset:\n{e}")
            return

        # Remove from internal list and table
        self._presets.pop(row)
        self._favorites.discard(name)
        self._save_favorites()
        self.table.removeRow(row)

        # If no presets remain, close the dialog
        if self.table.rowCount() == 0:
            self._deleted_last = True
            self.reject()


# ---------------------------------------------------------------------------
# Command history dialog
# ---------------------------------------------------------------------------


class HistoryDialog(QDialog):
    """Modal dialog for browsing and restoring command history entries."""

    def __init__(self, tool_name: str, history: list[dict], parent=None):
        super().__init__(parent)
        self.tool_name = tool_name
        self.history = history
        self.selected_entry: dict | None = None

        self.setWindowTitle(f"Command History \u2014 {tool_name}")
        self.resize(700, 450)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # Search bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter history...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        self.search_bar.installEventFilter(self)
        layout.addWidget(self.search_bar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Exit", "Command", "Notes", "Time", "Age"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setCascadingSectionResizes(True)
        self.table.horizontalHeader().setMinimumSectionSize(50)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Interactive
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(2, 150)
        _wire_last_column(self, self.table)
        _orig_resize = self.table.resizeEvent
        self.table.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self.table))
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.table, 1)

        # Empty-state label (shown when history is empty)
        self.empty_label = QLabel(
            f"No command history for {tool_name} yet.\n"
            "Runs will appear here after they complete."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f"color: {DARK_COLORS['disabled']}; font-size: 13px;")
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label, 1)

        # Buttons
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        self.restore_btn = QPushButton("Restore")
        self.restore_btn.setEnabled(False)
        self.restore_btn.clicked.connect(self._on_restore)
        btn_bar.addWidget(self.restore_btn)
        self.clear_btn = QPushButton("Clear History")
        self.clear_btn.clicked.connect(self._on_clear)
        btn_bar.addWidget(self.clear_btn)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_bar.addWidget(self.close_btn)
        layout.addLayout(btn_bar)

        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        self._populate()

    def _populate(self) -> None:
        """Fill the table with history entries."""
        self.table.setRowCount(len(self.history))
        now = time.time()
        for row, entry in enumerate(self.history):
            # Exit code column
            exit_code = entry.get("exit_code", -1)
            is_crashed = entry.get("crashed", False)
            error_token = entry.get("error")
            if error_token:
                _error_labels = {
                    "failed_to_start": "not found",
                    "crashed": "crashed",
                    "timed_out": "timed out",
                    "write_error": "I/O error",
                    "read_error": "I/O error",
                }
                exit_item = QTableWidgetItem(_error_labels.get(error_token, "error"))
                exit_item.setForeground(QColor("#dd3333"))
                exit_item.setToolTip(f"Process error: {error_token}")
            elif is_crashed:
                exit_item = QTableWidgetItem("crashed")
                exit_item.setForeground(QColor("#dd3333"))
                exit_item.setToolTip(f"Process crashed (raw exit code: {exit_code})")
            else:
                exit_item = QTableWidgetItem(str(exit_code))
                exit_item.setForeground(
                    QColor("#22bb45") if exit_code == 0 else QColor("#dd3333")
                )
            self.table.setItem(row, 0, exit_item)

            # Command column
            display = entry.get("display", "")
            if entry.get("_oversized"):
                display = f"{display}  [entry too large to restore]"
            cmd_item = QTableWidgetItem(display)
            if entry.get("_oversized"):
                cmd_item.setForeground(QColor("#888888"))
                cmd_item.setToolTip(
                    "This entry exceeded the history size cap. "
                    "The command line is preserved but the form values were dropped."
                )
            self.table.setItem(row, 1, cmd_item)

            # Notes column
            notes = entry.get("notes", "") or ""
            notes_item = QTableWidgetItem(notes)
            if notes:
                notes_item.setToolTip(notes)
            self.table.setItem(row, 2, notes_item)

            # Time column — "Mar 31, 14:23"
            ts = entry.get("timestamp", 0)
            try:
                dt = datetime.datetime.fromtimestamp(ts)
                time_str = dt.strftime("%b %d, %H:%M")
            except (OSError, ValueError):
                time_str = "?"
            self.table.setItem(row, 3, QTableWidgetItem(time_str))

            # Age column — relative time
            age_secs = max(0, now - ts)
            if age_secs < 60:
                age_str = f"{int(age_secs)}s ago"
            elif age_secs < 3600:
                age_str = f"{int(age_secs // 60)}m ago"
            elif age_secs < 86400:
                age_str = f"{int(age_secs // 3600)}h ago"
            else:
                age_str = f"{int(age_secs // 86400)}d ago"
            self.table.setItem(row, 4, QTableWidgetItem(age_str))

        self.table.resizeColumnToContents(3)
        _fit_last_column(self.table)

        empty = len(self.history) == 0
        self.table.setVisible(not empty)
        self.search_bar.setVisible(not empty)
        self.empty_label.setVisible(empty)
        self.clear_btn.setEnabled(not empty)

    def _on_selection(self) -> None:
        """Enable/disable Restore button based on selection."""
        if not self.table.selectedItems():
            self.restore_btn.setEnabled(False)
            return
        row = self.table.currentRow()
        if 0 <= row < len(self.history) and self.history[row].get("_oversized"):
            self.restore_btn.setEnabled(False)
        else:
            self.restore_btn.setEnabled(True)

    def _on_double_click(self) -> None:
        """Restore the double-clicked entry and close."""
        row = self.table.currentRow()
        if 0 <= row < len(self.history):
            if self.history[row].get("_oversized"):
                return
            self.selected_entry = self.history[row]
            self.accept()

    def _on_restore(self) -> None:
        """Restore the selected entry and close."""
        row = self.table.currentRow()
        if 0 <= row < len(self.history):
            if self.history[row].get("_oversized"):
                return
            self.selected_entry = self.history[row]
            self.accept()

    def _on_clear(self) -> None:
        """Clear all history after confirmation."""
        answer = QMessageBox.question(
            self, "Clear History",
            f"Remove all command history for {self.tool_name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            if self.parent():
                self.parent()._clear_history()
            self.reject()

    def _on_filter(self, text: str) -> None:
        """Hide rows that don't match the search text (case-insensitive substring).

        Searches the Command column (1) and the Notes column (2).
        """
        needle = text.lower()
        for row in range(self.table.rowCount()):
            cmd_item = self.table.item(row, 1)
            notes_item = self.table.item(row, 2)
            cmd_text = cmd_item.text().lower() if cmd_item else ""
            notes_text = notes_item.text().lower() if notes_item else ""
            match = (needle in cmd_text) or (needle in notes_text)
            self.table.setRowHidden(row, bool(needle) and not match)

    def _on_context_menu(self, pos) -> None:
        """Show right-click context menu with the Edit Notes action."""
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.history):
            return
        menu = QMenu(self.table)
        menu.addAction("Edit notes…", lambda: self._edit_notes(row))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _edit_notes(self, row: int) -> None:
        """Open a modal dialog to edit the notes for the given row."""
        if row < 0 or row >= len(self.history):
            return
        entry = self.history[row]
        current = entry.get("notes", "") or ""

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Notes")
        dlg.resize(400, 200)
        d_layout = QVBoxLayout(dlg)
        editor = QPlainTextEdit(dlg)
        editor.setPlainText(current)
        d_layout.addWidget(editor, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK", dlg)
        cancel_btn = QPushButton("Cancel", dlg)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        d_layout.addLayout(btn_row)
        dlg._notes_editor = editor  # expose for tests

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_notes = editor.toPlainText()
        parent = self.parent()
        if parent is not None and hasattr(parent, "_update_history_notes"):
            parent._update_history_notes(entry.get("timestamp"), new_notes)
        entry["notes"] = new_notes
        new_item = QTableWidgetItem(new_notes)
        if new_notes:
            new_item.setToolTip(new_notes)
        self.table.setItem(row, 2, new_item)

    def eventFilter(self, obj, event):
        if obj is self.search_bar and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                if self.search_bar.text():
                    self.search_bar.clear()
                    return True
        return super().eventFilter(obj, event)


# ---------------------------------------------------------------------------
# Cascade history dialog
# ---------------------------------------------------------------------------

# Status glyph mapping: status -> (glyph_char, color_hex)
_CASCADE_STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "completed":    ("+", "#22bb45"),
    "stopped":      ("-", "#dd8800"),
    "error_halted": ("!", "#dd3333"),
    "crashed":      ("x", "#dd3333"),
    "running":      ("~", "#3388dd"),
}


class CascadeHistoryDialog(QDialog):
    """Modal dialog for browsing cascade execution history.

    Displays cascade runs as parent rows and their steps as child rows in a
    QTreeWidget.  The dialog works entirely on the in-memory *history* list
    passed to the constructor — it never loads from QSettings itself.
    """

    def __init__(self, history: list[dict], parent=None):
        super().__init__(parent)
        self.history = list(history)  # operate on a mutable copy
        self.restore_result: dict | None = None  # set by restore handlers
        self.setWindowTitle("Cascade History")
        self.resize(700, 450)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # --- Filter bar ---
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter by cascade name...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        self.search_bar.installEventFilter(self)
        layout.addWidget(self.search_bar)

        # --- Tree widget ---
        # Columns: Status | Name | Command | Steps | Started
        # Parent rows: glyph | cascade name | (blank) | ran/total | timestamp
        # Step rows:   glyph/blank | tool-preset | command | N/Y | timestamp
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Status", "Name", "Command", "Steps", "Started"])
        self.tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setMinimumSectionSize(30)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(0, 40)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 180)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(2, 240)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(3, 60)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        _wire_last_column(self, self.tree)
        _orig_resize = self.tree.resizeEvent
        self.tree.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self.tree))
        layout.addWidget(self.tree, 1)

        # --- Empty-state label ---
        self.empty_label = QLabel(
            "No cascade history yet.\n"
            "Runs will appear here after they complete."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f"color: {DARK_COLORS['disabled']}; font-size: 13px;")
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label, 1)

        # --- Button row ---
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        self.restore_step_btn = QPushButton("Restore Step")
        self.restore_step_btn.setEnabled(False)
        self.restore_step_btn.clicked.connect(self._on_restore_step)
        btn_bar.addWidget(self.restore_step_btn)

        self.restore_cascade_btn = QPushButton("Restore Cascade")
        self.restore_cascade_btn.setEnabled(False)
        self.restore_cascade_btn.clicked.connect(self._on_restore_cascade)
        btn_bar.addWidget(self.restore_cascade_btn)

        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export)
        btn_bar.addWidget(self.export_btn)

        self.export_all_btn = QPushButton("Export All")
        self.export_all_btn.setEnabled(False)
        self.export_all_btn.clicked.connect(self._on_export_all)
        btn_bar.addWidget(self.export_all_btn)

        self.delete_btn = QPushButton("Delete Entry")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete)
        btn_bar.addWidget(self.delete_btn)

        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self._on_clear)
        btn_bar.addWidget(self.clear_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_bar.addWidget(self.close_btn)

        layout.addLayout(btn_bar)

        self.tree.selectionModel().selectionChanged.connect(self._on_selection)
        self._populate()

    # ----- tree population -------------------------------------------------

    def _populate(self) -> None:
        """Fill the tree with cascade history entries."""
        self.tree.clear()

        for entry in self.history:
            cascade_item = QTreeWidgetItem()

            # Col 0 — status glyph
            status = entry.get("status", "")
            glyph, color = _CASCADE_STATUS_GLYPHS.get(status, ("?", "#888888"))
            cascade_item.setText(0, glyph)
            cascade_item.setForeground(0, QColor(color))
            glyph_font = cascade_item.font(0)
            glyph_font.setBold(True)
            cascade_item.setFont(0, glyph_font)
            cascade_item.setToolTip(0, status or "unknown")

            # Col 1 — cascade name
            name = entry.get("name", "")
            if name:
                cascade_item.setText(1, name)
            else:
                cascade_item.setText(1, "(untitled)")
                italic_font = cascade_item.font(1)
                italic_font.setItalic(True)
                cascade_item.setFont(1, italic_font)

            # Col 3 — steps completed / total
            steps = entry.get("steps", [])
            ran = sum(
                1 for s in steps
                if s.get("exit_code") is not None or s.get("error") is not None
            )
            cascade_item.setText(3, f"{ran}/{len(steps)}")

            # Col 4 — started timestamp
            ts = entry.get("started_at", 0)
            try:
                dt = datetime.datetime.fromtimestamp(ts)
                cascade_item.setText(4, dt.strftime("%b %d, %H:%M"))
            except (OSError, ValueError):
                cascade_item.setText(4, "?")

            # Store the entry dict on the item for later retrieval
            cascade_item.setData(0, Qt.ItemDataRole.UserRole, entry)

            # --- child step rows ---
            total_steps = len(steps)
            for step in steps:
                step_item = QTreeWidgetItem()

                # Col 0 — step-level status glyph (blank for success/skipped)
                error = step.get("error")
                exit_code = step.get("exit_code")
                if error:
                    glyph_ch = "x" if error == "crashed" else "!"
                    step_item.setText(0, glyph_ch)
                    step_item.setForeground(0, QColor("#dd3333"))
                    step_font = step_item.font(0)
                    step_font.setBold(True)
                    step_item.setFont(0, step_font)
                elif exit_code is not None and exit_code != 0:
                    step_item.setText(0, "!")
                    step_item.setForeground(0, QColor("#dd3333"))
                    step_font = step_item.font(0)
                    step_font.setBold(True)
                    step_item.setFont(0, step_font)
                # else: success or skipped — col 0 stays blank

                # Col 1 — tool name with optional preset name
                tool_name = step.get("tool_name", "")
                preset_path = step.get("preset_path")
                if preset_path:
                    step_item.setText(1, f"{tool_name} - {Path(preset_path).stem}")
                else:
                    step_item.setText(1, tool_name)

                # Col 2 — command string
                step_item.setText(2, step.get("command", ""))

                # Col 3 — step progress N/total
                step_idx = step.get("index", 0) + 1
                step_item.setText(3, f"{step_idx}/{total_steps}")

                # Col 4 — started timestamp
                step_ts = step.get("started_at")
                if step_ts is not None:
                    try:
                        step_dt = datetime.datetime.fromtimestamp(step_ts)
                        step_item.setText(4, step_dt.strftime("%b %d, %H:%M"))
                    except (OSError, ValueError):
                        step_item.setText(4, "?")

                # Store step dict
                step_item.setData(0, Qt.ItemDataRole.UserRole, step)

                cascade_item.addChild(step_item)

            self.tree.addTopLevelItem(cascade_item)

        # Empty-state visibility
        empty = len(self.history) == 0
        self.tree.setVisible(not empty)
        self.search_bar.setVisible(not empty)
        self.empty_label.setVisible(empty)
        self.clear_btn.setEnabled(not empty)
        self.export_all_btn.setEnabled(not empty)
        self._on_selection()

    # ----- selection & enablement ------------------------------------------

    def _on_selection(self) -> None:
        """Update button enablement based on current tree selection."""
        items = self.tree.selectedItems()
        if not items:
            self.restore_step_btn.setEnabled(False)
            self.restore_cascade_btn.setEnabled(False)
            self.export_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            return
        item = items[0]
        is_step = item.parent() is not None
        self.restore_step_btn.setEnabled(is_step)
        self.restore_cascade_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.delete_btn.setEnabled(True)

    # ----- button handlers -------------------------------------------------

    def _on_restore_step(self) -> None:
        """Restore the selected step's tool and preset into the main form."""
        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]
        if item.parent() is None:
            return  # only step (child) rows
        step = item.data(0, Qt.ItemDataRole.UserRole)
        if step is None:
            return
        tool_path = step.get("tool_path")
        if not tool_path or not Path(tool_path).exists():
            QMessageBox.warning(
                self,
                "Tool Not Found",
                f"The tool schema file no longer exists:\n{tool_path or '(unknown)'}",
            )
            return
        self.restore_result = {"action": "step", "step": step}
        self.accept()

    def _on_restore_cascade(self) -> None:
        """Restore the selected cascade config into the cascade sidebar."""
        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]
        if item.parent() is not None:
            item = item.parent()
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        config = entry.get("config")
        if config is None:
            QMessageBox.warning(
                self,
                "No Configuration",
                "This run entry does not contain a configuration snapshot.",
            )
            return
        # Check for missing tool files
        missing: list[str] = []
        for i, slot in enumerate(config.get("slots", [])):
            tp = slot.get("tool_path")
            if tp and not Path(tp).exists():
                missing.append(f"Slot {i + 1}: {Path(tp).name}")
        if missing:
            QMessageBox.warning(
                self,
                "Missing Tools",
                "The following tools no longer exist:\n\n"
                + "\n".join(missing)
                + "\n\nThese slots will be empty in the restored cascade.",
            )
        self.restore_result = {"action": "cascade", "config": config}
        self.accept()

    def _on_export(self) -> None:
        """Export the selected cascade run as JSON.

        Resolves step selections to their parent cascade.  Writes JSON with
        ``_format`` / ``_version`` envelope using the atomic ``.tmp`` +
        ``os.replace`` pattern from ``_atomic_write_json``.
        """
        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]
        # Resolve to parent cascade row
        if item.parent() is not None:
            item = item.parent()
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:
            return

        name = entry.get("name", "")
        default_name = f"{name}_history.json" if name else "cascade_history.json"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Cascade History", default_name, "JSON files (*.json)",
        )
        if not dest:
            return

        data = {
            "_format": "scaffold_cascade_history",
            "_version": CASCADE_HISTORY_VERSION,
            "entries": [entry],
        }
        try:
            _atomic_write_json(Path(dest), data)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not write to {dest}:\n{exc}",
            )

    def _on_export_all(self) -> None:
        """Export the entire cascade history as JSON.

        Writes all entries regardless of selection.  Same ``_format`` /
        ``_version`` envelope and atomic-write pattern as ``_on_export``.
        """
        if not self.history:
            return

        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Cascade History", "cascade_history_all.json",
            "JSON files (*.json)",
        )
        if not dest:
            return

        data = {
            "_format": "scaffold_cascade_history",
            "_version": CASCADE_HISTORY_VERSION,
            "entries": list(self.history),
        }
        try:
            _atomic_write_json(Path(dest), data)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not write to {dest}:\n{exc}",
            )

    def _on_delete(self) -> None:
        """Delete the selected cascade entry from in-memory history and
        refresh the tree.  If a step row is selected, deletes the parent
        cascade entry (steps are not independently deletable).
        """
        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]
        # Resolve to parent cascade row
        is_child = item.parent() is not None
        if is_child:
            item = item.parent()
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is not None and entry in self.history:
            msg = (
                "Deleting this step will remove the entire parent "
                "cascade entry from history. Continue?"
                if is_child
                else "Delete this cascade history entry? This cannot be undone."
            )
            answer = QMessageBox.question(
                self,
                "Delete Cascade Entry",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.history.remove(entry)
        self._populate()

    def _on_clear(self) -> None:
        """Clear all cascade history after confirmation."""
        answer = QMessageBox.question(
            self,
            "Clear Cascade History",
            "Remove all cascade history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self._populate()

    # ----- filter -----------------------------------------------------------

    def _on_filter(self, text: str) -> None:
        """Hide rows whose visible columns don't match *text* (case-insensitive
        substring).  Both cascade and step rows are checked.  A matching child
        keeps its parent visible; a matching parent keeps all children visible.
        """
        needle = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            cascade_item = self.tree.topLevelItem(i)
            if not needle:
                cascade_item.setHidden(False)
                for j in range(cascade_item.childCount()):
                    cascade_item.child(j).setHidden(False)
                continue

            # Check cascade-level columns
            cascade_match = any(
                needle in (cascade_item.text(c) or "").lower()
                for c in range(self.tree.columnCount())
            )

            # Check each child step row
            any_child_match = False
            for j in range(cascade_item.childCount()):
                child = cascade_item.child(j)
                child_match = any(
                    needle in (child.text(c) or "").lower()
                    for c in range(self.tree.columnCount())
                )
                child.setHidden(not child_match and not cascade_match)
                if child_match:
                    any_child_match = True

            cascade_item.setHidden(not cascade_match and not any_child_match)

    # ----- event filter (Esc clears search) ---------------------------------

    def eventFilter(self, obj, event):
        if obj is self.search_bar and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                if self.search_bar.text():
                    self.search_bar.clear()
                    return True
        return super().eventFilter(obj, event)


# ---------------------------------------------------------------------------
# Cascade headless runner (CLI --run-cascade entry point)
# ---------------------------------------------------------------------------


class CascadeHeadlessAbort(Exception):
    """Raised when a headless cascade run must abort due to a fatal modal-dialog scenario."""


_HEADLESS_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CascadeHeadlessRunner:
    """Run a saved cascade from the command line without showing the GUI.

    Hosts a hidden ``MainWindow`` so the existing cascade execution path
    (``CascadeSidebar._on_run_chain`` and friends) runs unchanged. Modal
    dialogs and history writes are patched out for the duration of the
    run; per-step ``QProcess`` output is streamed to the parent's
    ``stdout``/``stderr`` line-by-line with a ``[step N: tool]`` prefix.
    Returns an exit code per the documented spec (0/1/2/3/130).
    """

    # QSettings keys that headless runs would otherwise mutate as a side
    # effect of constructing MainWindow + running cascade steps.  Snapshot
    # at run start and restore at run end so a headless invocation cannot
    # disturb user state (session/last_tool, recent tools, cascade slots,
    # cascade history, etc.).
    _SETTINGS_KEYS_TO_PRESERVE = (
        "session/last_tool",
        "picker/recent_tools",
        "cascade/slots",
        "cascade/loop_mode",
        "cascade/stop_on_error",
        "cascade/variables",
        "cascade/current_name",
        "cascade/visible",
        "cascade_history/entries",
        "cascade_history/_version",
    )

    def __init__(
        self,
        cascade_path: str,
        variables: dict | None = None,
        loop_count: int = 1,
        summary_path: str | None = None,
    ) -> None:
        self.cascade_path = str(cascade_path)
        self.variables: dict = dict(variables or {})
        self.loop_count = max(1, int(loop_count))
        self.summary_path = str(summary_path) if summary_path else None

        self._main_window = None
        self._aborted = False
        self._abort_reason: str | None = None
        self._sigint = False
        self._has_nonzero_exit = False
        self._cascade_name = ""
        self._cascade_stop_on_error = False
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._final_status: str | None = None
        self._stdout_buf = ""
        self._stderr_buf = ""
        self._step_records: list[dict] = []
        self._current_loop_index = 0
        # restoration list for class-level patches
        self._patches: list[tuple] = []
        self._settings_snapshot: dict | None = None
        # populated by patched _append_cascade_run / _update_cascade_run
        self._captured_runs: list[dict] = []

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        app = QApplication.instance() or QApplication(sys.argv[:1])

        self._snapshot_settings()

        prev_suppress = MainWindow._suppress_welcome_dialog
        MainWindow._suppress_welcome_dialog = True
        try:
            try:
                self._main_window = MainWindow()
            # broadly defensive — MainWindow construction touches QSettings, QTimer, file
            # I/O during recovery checks; any failure here aborts headless boot rather
            # than crashing.
            except Exception as exc:
                print(
                    f"scaffold: failed to construct MainWindow: {exc}",
                    file=sys.stderr,
                )
                self._restore_settings()
                return 1
        finally:
            MainWindow._suppress_welcome_dialog = prev_suppress

        try:
            return self._run_inner(app)
        finally:
            self._uninstall_patches()
            try:
                if self._main_window is not None:
                    self._main_window._teardown_process()
            # broadly defensive — teardown is best-effort cleanup; an error here cannot
            # be reported to the caller and must not change the exit code.
            except Exception:
                pass
            try:
                if self._main_window is not None:
                    self._main_window.close()
                    self._main_window.deleteLater()
            except (AttributeError, RuntimeError):
                pass
            self._restore_settings()

    # -- inner run ---------------------------------------------------------

    def _run_inner(self, app) -> int:
        mw = self._main_window

        try:
            data = _read_user_json(self.cascade_path)
        except (RuntimeError, OSError, json.JSONDecodeError) as exc:
            print(
                f"scaffold: failed to read cascade {self.cascade_path}: {exc}",
                file=sys.stderr,
            )
            return 1

        if not isinstance(data, dict) or data.get("_format") != "scaffold_cascade":
            fmt = data.get("_format") if isinstance(data, dict) else None
            print(
                f"scaffold: not a valid cascade file "
                f"(expected '_format':'scaffold_cascade', got {fmt!r})",
                file=sys.stderr,
            )
            return 1

        self._cascade_name = data.get("name") or Path(self.cascade_path).stem
        self._cascade_stop_on_error = bool(data.get("stop_on_error", False))

        missing_tools, missing_presets = _check_cascade_dependencies(data)
        if missing_tools or missing_presets:
            lines = ["scaffold: cascade has missing dependencies:"]
            for t in missing_tools:
                lines.append(f"  missing tool: {t}")
            for p in missing_presets:
                lines.append(f"  missing preset: {p}")
            print("\n".join(lines), file=sys.stderr)
            return 1

        cascade_vars = [v for v in data.get("variables", []) if isinstance(v, dict)]
        required_flags = [v.get("flag", "") for v in cascade_vars if v.get("flag")]
        missing_flags = [f for f in required_flags if f not in self.variables]
        if missing_flags:
            print(
                f"scaffold: missing required cascade variables: "
                f"{', '.join(missing_flags)}",
                file=sys.stderr,
            )
            return 1

        self._install_patches()
        self._install_streaming_hooks()

        try:
            mw.cascade_dock._import_cascade_data(data)
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            print(f"scaffold: invalid cascade data: {exc}", file=sys.stderr)
            return 1

        # The cascade's loop_enabled is intentionally ignored in headless mode;
        # outer loop count is controlled exclusively via --loop.
        mw.cascade_dock._loop_enabled = False

        old_sigint = None
        try:
            old_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        # signal.signal raises ValueError if not in main thread; OSError on some platforms.
        except (ValueError, OSError):
            pass

        self._started_at = time.time()

        try:
            for loop_idx in range(self.loop_count):
                if self._sigint or self._aborted:
                    break
                self._current_loop_index = loop_idx

                started = self._run_one_iteration()
                if not started:
                    break

                finish = mw.cascade_dock._cascade_finish_status
                if finish:
                    self._final_status = finish
                if finish == "error_halted" and self._cascade_stop_on_error:
                    break
                if self._sigint or self._aborted:
                    break
        finally:
            if old_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, old_sigint)
                except (ValueError, OSError):
                    pass
            self._finished_at = time.time()

        if self._sigint:
            final_status = "interrupted"
            exit_code = 130
        elif self._aborted:
            final_status = "failed"
            exit_code = 1
        elif self._final_status == "error_halted" and self._cascade_stop_on_error:
            final_status = "stopped"
            exit_code = 2
        elif self._has_nonzero_exit:
            final_status = "completed"
            exit_code = 3
        else:
            final_status = "completed"
            exit_code = 0

        if self.summary_path:
            try:
                self._write_summary(final_status)
            except (OSError, TypeError, ValueError) as exc:
                print(
                    f"scaffold: failed to write summary {self.summary_path}: {exc}",
                    file=sys.stderr,
                )

        return exit_code

    # -- settings snapshot/restore ----------------------------------------

    def _snapshot_settings(self) -> None:
        s = _create_settings()
        snap: dict = {}
        for key in self._SETTINGS_KEYS_TO_PRESERVE:
            snap[key] = (s.value(key, None), s.contains(key))
        self._settings_snapshot = snap

    def _restore_settings(self) -> None:
        if self._settings_snapshot is None:
            return
        s = _create_settings()
        for key, (value, existed) in self._settings_snapshot.items():
            if existed:
                s.setValue(key, value)
            else:
                s.remove(key)
        s.sync()
        self._settings_snapshot = None

    # -- signal handler ---------------------------------------------------

    def _on_sigint(self, signum, frame) -> None:
        self._sigint = True
        try:
            if self._main_window is not None:
                self._main_window.cascade_dock._on_stop_chain()
        # broadly defensive — signal handler must never raise; chain teardown errors
        # are deferred to normal cleanup.
        except Exception:
            pass

    # -- patches -----------------------------------------------------------

    def _install_patches(self) -> None:
        runner = self
        supplied = dict(self.variables)

        original_exec = CascadeVariableDialog.exec
        original_get_values = CascadeVariableDialog.get_values

        def patched_exec(_self):
            return QDialog.DialogCode.Accepted

        def patched_get_values(_self):
            return dict(supplied)

        CascadeVariableDialog.exec = patched_exec
        CascadeVariableDialog.get_values = patched_get_values
        self._patches.append((CascadeVariableDialog, "exec", original_exec))
        self._patches.append((CascadeVariableDialog, "get_values", original_get_values))

        for level in ("warning", "critical", "information", "question"):
            original = getattr(QMessageBox, level)
            self._patches.append((QMessageBox, level, original))

        def make_patch(level: str):
            def patched(*args, **kwargs):
                title = args[1] if len(args) > 1 else kwargs.get("title", "")
                text = args[2] if len(args) > 2 else kwargs.get("text", "")
                print(
                    f"scaffold: cascade aborted by modal dialog "
                    f"({level}): {title} — {text}",
                    file=sys.stderr,
                )
                runner._aborted = True
                runner._abort_reason = f"{level}: {title}"
                if level == "question":
                    return QMessageBox.StandardButton.No
                return QMessageBox.StandardButton.Ok
            return patched

        for level in ("warning", "critical", "information", "question"):
            setattr(QMessageBox, level, make_patch(level))

        mw = self._main_window
        # Instance-level patches: suppress QSettings writes from history and slot
        # persistence, and capture per-run step data through _update_cascade_run
        # (which the cascade dock calls with the full step list right before
        # _chain_cleanup nulls _cascade_steps).  These are restored implicitly
        # when the MainWindow is destroyed at end of run.
        self._captured_runs: list[dict] = []

        def patched_append(entry):
            if isinstance(entry, dict) and entry.get("id"):
                self._captured_runs.append({
                    "id": entry["id"],
                    "loop_index": self._current_loop_index,
                    "steps": [],
                })

        def patched_update(run_id, updates):
            if not run_id or not isinstance(updates, dict):
                return
            for run in self._captured_runs:
                if run["id"] == run_id:
                    if "steps" in updates:
                        run["steps"] = [dict(s) for s in (updates["steps"] or [])]
                    break

        mw._append_cascade_run = patched_append
        mw._update_cascade_run = patched_update
        mw.cascade_dock._save_cascade = lambda: None

    def _uninstall_patches(self) -> None:
        for target, attr, original in self._patches:
            try:
                setattr(target, attr, original)
            except (AttributeError, TypeError):
                pass
        self._patches = []

    # -- streaming ---------------------------------------------------------

    def _install_streaming_hooks(self) -> None:
        mw = self._main_window
        runner = self

        def patched_stdout_ready():
            proc = mw.process
            if proc is None:
                return
            text = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
            mw._last_run_stdout += text
            runner._stream("stdout", text)

        def patched_stderr_ready():
            proc = mw.process
            if proc is None:
                return
            text = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace")
            mw._last_run_stderr += text
            runner._stream("stderr", text)

        mw._on_stdout_ready = patched_stdout_ready
        mw._on_stderr_ready = patched_stderr_ready

        original_on_finished = mw._on_finished

        def patched_on_finished(exit_code, exit_status):
            runner._flush_partial_lines()
            original_on_finished(exit_code, exit_status)

        mw._on_finished = patched_on_finished

    def _line_prefix(self) -> str | None:
        if self._main_window is None:
            return None
        dock = self._main_window.cascade_dock
        if not (0 <= dock._chain_current < len(dock._chain_queue)):
            return None
        slot_idx = dock._chain_queue[dock._chain_current]
        if not (0 <= slot_idx < len(dock._slots)):
            return None
        slot = dock._slots[slot_idx]
        tp = slot.get("tool_path")
        tool_name = Path(tp).stem if tp else "?"
        return f"[step {dock._chain_current + 1}: {tool_name}]"

    def _stream(self, which: str, text: str) -> None:
        prefix = self._line_prefix()
        if prefix is None:
            return
        if which == "stdout":
            self._stdout_buf += text
            stream = sys.stdout
            tag = ""
            buf_attr = "_stdout_buf"
        else:
            self._stderr_buf += text
            stream = sys.stderr
            tag = "(err) "
            buf_attr = "_stderr_buf"
        buf = getattr(self, buf_attr)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            stream.write(f"{prefix} {tag}{line}\n")
        setattr(self, buf_attr, buf)
        stream.flush()

    def _flush_partial_lines(self) -> None:
        prefix = self._line_prefix()
        if prefix is None:
            self._stdout_buf = ""
            self._stderr_buf = ""
            return
        if self._stdout_buf:
            sys.stdout.write(f"{prefix} {self._stdout_buf}\n")
            sys.stdout.flush()
            self._stdout_buf = ""
        if self._stderr_buf:
            sys.stderr.write(f"{prefix} (err) {self._stderr_buf}\n")
            sys.stderr.flush()
            self._stderr_buf = ""

    # -- iteration ---------------------------------------------------------

    def _run_one_iteration(self) -> bool:
        """Run a single cascade pass; block until ``_chain_state == CHAIN_IDLE``.

        Returns False if the run aborted before starting (e.g. modal-dialog
        fail-fast); True otherwise (including normal completion and stop_on_error).
        """
        mw = self._main_window
        dock = mw.cascade_dock

        dock._chain_state = CHAIN_IDLE
        dock._chain_paused = False
        dock._cascade_finish_status = None
        dock._cascade_steps = []
        dock._cascade_run_id = None

        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(50)

        def tick():
            if (dock._chain_state == CHAIN_IDLE
                    or self._sigint
                    or self._aborted):
                timer.stop()
                loop.quit()

        timer.timeout.connect(tick)

        try:
            try:
                dock._on_run_chain()
            except CascadeHeadlessAbort as exc:
                self._aborted = True
                self._abort_reason = str(exc)
                return False
        # broadly defensive — _on_run_chain → _chain_advance → _chain_execute_current
        # touches QProcess, file I/O, validation; any unexpected error class aborts
        # the run rather than leaving Qt in a half-started state.
        except Exception as exc:
            print(
                f"scaffold: cascade run failed to start: {exc}",
                file=sys.stderr,
            )
            self._aborted = True
            return False

        # If _on_run_chain returned without entering an active state (e.g. the
        # cascade has no valid steps) the dock is already idle; skip the wait
        # and proceed to bookkeeping.
        if dock._chain_state != CHAIN_IDLE:
            timer.start()
            loop.exec()
            timer.stop()

        self._collect_step_records()
        return True

    def _collect_step_records(self) -> None:
        # Drain captured runs into our per-step records and reset the buffer
        # so the next iteration starts clean.  _captured_runs is fed by the
        # patched _append_cascade_run / _update_cascade_run on the MainWindow.
        for run in self._captured_runs:
            loop_idx = run.get("loop_index", 0)
            for step in run.get("steps", []):
                started = step.get("started_at") or 0.0
                finished = step.get("finished_at") or 0.0
                duration = finished - started if (started and finished) else 0.0
                rec = {
                    "loop_index": loop_idx,
                    "step_index": step.get("index", -1),
                    "tool": step.get("tool_name", ""),
                    "command": step.get("command", "") or "",
                    "exit_code": step.get("exit_code"),
                    "duration_seconds": duration,
                    "captures": dict(step.get("captures", {}) or {}),
                    "error": step.get("error"),
                }
                self._step_records.append(rec)
                ec = step.get("exit_code")
                if ec is not None and ec != 0:
                    self._has_nonzero_exit = True
                if step.get("error"):
                    self._has_nonzero_exit = True
        self._captured_runs = []

    # -- summary -----------------------------------------------------------

    def _write_summary(self, final_status: str) -> None:
        def _iso(t):
            if not t:
                return None
            return datetime.datetime.fromtimestamp(
                t, tz=datetime.timezone.utc
            ).isoformat()

        duration = 0.0
        if self._started_at and self._finished_at:
            duration = self._finished_at - self._started_at

        summary = {
            "cascade": self._cascade_name,
            "status": final_status,
            "started_at": _iso(self._started_at),
            "finished_at": _iso(self._finished_at),
            "duration_seconds": duration,
            "loop_count": self.loop_count,
            "steps": self._step_records,
        }
        Path(self.summary_path).write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )


def _parse_run_cascade_args(argv: list[str]) -> tuple[dict, str | None]:
    """Parse ``--run-cascade`` and friends out of *argv*.

    Returns ``(parsed, error)``: on success ``parsed`` is a dict with keys
    ``cascade_path`` (str), ``variables`` (dict), ``loop_count`` (int | None),
    ``summary_path`` (str | None); ``error`` is ``None``. On failure
    ``parsed`` is ``{}`` and ``error`` is a usage message suitable for
    printing to stderr.

    ``loop_count`` is ``None`` when ``--loop`` was not on the command line, so
    callers can distinguish "user didn't pass --loop" from "user passed
    --loop 1". main() uses this to enforce explicit ``--loop N`` when the
    cascade JSON has ``loop_mode`` set.
    """
    cascade_path: str | None = None
    variables: dict[str, str] = {}
    file_vars: dict[str, str] = {}
    loop_count: int | None = None
    summary_path: str | None = None

    KNOWN = {"--run-cascade", "--var", "--vars", "--loop", "--summary"}
    i = 0
    # Skip everything before --run-cascade (already validated by caller)
    while i < len(argv) and argv[i] != "--run-cascade":
        i += 1
    while i < len(argv):
        a = argv[i]
        if a == "--run-cascade":
            if i + 1 >= len(argv):
                return {}, "--run-cascade requires a path argument"
            cascade_path = argv[i + 1]
            i += 2
        elif a == "--var":
            if i + 1 >= len(argv):
                return {}, "--var requires NAME=VALUE"
            spec = argv[i + 1]
            if "=" not in spec:
                return {}, f"--var requires NAME=VALUE format (got {spec!r})"
            name, _, value = spec.partition("=")
            if not name:
                return {}, "--var name cannot be empty"
            if not _HEADLESS_VAR_NAME_RE.match(name):
                return {}, (
                    f"--var name {name!r} is invalid "
                    f"(must match [A-Za-z_][A-Za-z0-9_]*)"
                )
            variables[name] = value
            i += 2
        elif a == "--vars":
            if i + 1 >= len(argv):
                return {}, "--vars requires a JSON file path"
            try:
                loaded = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return {}, f"--vars failed to read {argv[i + 1]!r}: {exc}"
            if not isinstance(loaded, dict):
                return {}, f"--vars file must contain a JSON object (got {type(loaded).__name__})"
            for k, v in loaded.items():
                if not isinstance(k, str) or not _HEADLESS_VAR_NAME_RE.match(k):
                    return {}, (
                        f"--vars contains invalid name {k!r} "
                        f"(must match [A-Za-z_][A-Za-z0-9_]*)"
                    )
                file_vars[k] = "" if v is None else str(v)
            i += 2
        elif a == "--loop":
            if i + 1 >= len(argv):
                return {}, "--loop requires an integer argument"
            try:
                n = int(argv[i + 1])
            except ValueError:
                return {}, f"--loop requires an integer (got {argv[i + 1]!r})"
            if n < 1 or n > 1000:
                return {}, f"--loop must be in range 1..1000 (got {n})"
            loop_count = n
            i += 2
        elif a == "--summary":
            if i + 1 >= len(argv):
                return {}, "--summary requires a path argument"
            summary_path = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            return {}, f"unknown flag {a!r}"
        else:
            return {}, f"unexpected positional argument {a!r}"

    if not cascade_path:
        return {}, "--run-cascade requires a path argument"

    # Merge: --vars first, then --var overrides on conflict
    merged = dict(file_vars)
    merged.update(variables)

    return (
        {
            "cascade_path": cascade_path,
            "variables": merged,
            "loop_count": loop_count,
            "summary_path": summary_path,
        },
        None,
    )


# ---------------------------------------------------------------------------
# Cascade picker dialog
# ---------------------------------------------------------------------------

class CascadePickerDialog(QDialog):
    """Modal dialog showing a tree of tools and their presets for cascade slot assignment."""

    def __init__(self, slot_index: int, main_window, parent=None):
        super().__init__(parent or main_window)
        self.setWindowTitle(f"Select Cascade Step \u2014 Slot {slot_index + 1}")
        self.resize(500, 450)
        self.selected_tool: str | None = None
        self.selected_preset: str | None = None

        layout = QVBoxLayout(self)

        # Search bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter tools and presets...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        self.search_bar.installEventFilter(self)
        self._filter_cycle_index = -1
        layout.addWidget(self.search_bar)

        # Tree widget
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(clear_btn)
        self.select_btn = QPushButton("Select")
        self.select_btn.setEnabled(False)
        self.select_btn.clicked.connect(self._on_select)
        btn_row.addWidget(self.select_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        self._populate_tree()

    def _populate_tree(self) -> None:
        """Scan user + bundled tools and presets, build the tree.

        Mirrors ToolPicker.scan: user tools take precedence by filename
        when both directories contain the same name.
        """
        self.tree.clear()
        user_path = _tools_dir()
        bundled_path = _bundled_tools_dir()
        entries = []
        seen_names: set[str] = set()
        for root, source in ((user_path, "user"), (bundled_path, "bundled")):
            if not root.exists():
                continue
            for json_file in sorted(root.rglob("*.json")):
                if source == "bundled" and json_file.name in seen_names:
                    continue
                try:
                    data = load_tool(str(json_file))
                    errors = validate_tool(data)
                    if errors:
                        continue
                    data = normalize_tool(data)
                    available = _binary_in_path(data["binary"])
                    entries.append((str(json_file), data, available))
                    if source == "user":
                        seen_names.add(json_file.name)
                except RuntimeError:
                    continue

        # Sort: available first, then alphabetically
        entries.sort(key=lambda e: (0 if e[2] else 1, e[1]["tool"].lower()))

        for tool_path, data, available in entries:
            tool_name = data["tool"]
            presets_path = _presets_dir(tool_name)
            preset_files = sorted(presets_path.glob("*.json"))

            # Build parent node
            suffix = f" ({len(preset_files)} preset{'s' if len(preset_files) != 1 else ''})"
            if not available:
                suffix += " (not installed)"
            item = QTreeWidgetItem([f"{tool_name}{suffix}"])
            item.setData(0, Qt.ItemDataRole.UserRole, tool_path)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "tool")
            if not available:
                item.setForeground(0, QColor(COLOR_DIM))
            self.tree.addTopLevelItem(item)

            # Build preset children
            for pf in preset_files:
                child = QTreeWidgetItem([pf.stem])
                child.setData(0, Qt.ItemDataRole.UserRole, str(pf))
                child.setData(0, Qt.ItemDataRole.UserRole + 1, "preset")
                # Store parent tool path on the child for convenience
                child.setData(0, Qt.ItemDataRole.UserRole + 2, tool_path)
                item.addChild(child)

            if not preset_files:
                placeholder = QTreeWidgetItem(["(no presets)"])
                placeholder.setData(0, Qt.ItemDataRole.UserRole + 1, "placeholder")
                placeholder.setForeground(0, QColor(COLOR_DIM))
                item.addChild(placeholder)

    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        """Double-click handler: accept on preset nodes, toggle expand on tool nodes."""
        node_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if node_type == "preset":
            self.selected_tool = item.data(0, Qt.ItemDataRole.UserRole + 2)
            self.selected_preset = item.data(0, Qt.ItemDataRole.UserRole)
            self.accept()
        # tool or placeholder: do nothing (tree handles expand/collapse)

    def _on_select(self) -> None:
        """Select button: accept with current tree item (tool or preset)."""
        item = self.tree.currentItem()
        if not item:
            return
        node_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if node_type == "preset":
            self.selected_tool = item.data(0, Qt.ItemDataRole.UserRole + 2)
            self.selected_preset = item.data(0, Qt.ItemDataRole.UserRole)
            self.accept()
        elif node_type == "tool":
            self.selected_tool = item.data(0, Qt.ItemDataRole.UserRole)
            self.selected_preset = None
            self.accept()

    def _on_tree_selection_changed(self, current, previous) -> None:
        """Enable Select button when a valid item (tool or preset) is selected."""
        if not current:
            self.select_btn.setEnabled(False)
            return
        node_type = current.data(0, Qt.ItemDataRole.UserRole + 1)
        self.select_btn.setEnabled(node_type in ("tool", "preset"))

    def _on_clear(self) -> None:
        """Clear the slot assignment."""
        self.selected_tool = None
        self.selected_preset = None
        self.accept()

    def _on_filter(self, text: str) -> None:
        """Filter tree items by search text."""
        query = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            tool_item = self.tree.topLevelItem(i)
            tool_name = tool_item.text(0).lower()
            tool_matches = query in tool_name

            any_child_visible = False
            for j in range(tool_item.childCount()):
                child = tool_item.child(j)
                child_matches = query in child.text(0).lower()
                child.setHidden(bool(query) and not child_matches and not tool_matches)
                if not child.isHidden():
                    any_child_visible = True

            tool_item.setHidden(bool(query) and not tool_matches and not any_child_visible)
            if any_child_visible and query:
                tool_item.setExpanded(True)
        self._filter_cycle_index = -1

    def _visible_tree_items(self) -> list[QTreeWidgetItem]:
        """Return a flat list of visible, selectable tree items (tools and presets)."""
        items = []
        for i in range(self.tree.topLevelItemCount()):
            tool_item = self.tree.topLevelItem(i)
            if tool_item.isHidden():
                continue
            items.append(tool_item)
            if tool_item.isExpanded():
                for j in range(tool_item.childCount()):
                    child = tool_item.child(j)
                    if not child.isHidden():
                        node_type = child.data(0, Qt.ItemDataRole.UserRole + 1)
                        if node_type != "placeholder":
                            items.append(child)
        return items

    def _filter_next(self) -> None:
        """Select the next visible tree item (Enter in filter bar)."""
        visible = self._visible_tree_items()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index + 1) % len(visible)
        self.tree.setCurrentItem(visible[self._filter_cycle_index])
        self.tree.scrollToItem(visible[self._filter_cycle_index])

    def _filter_prev(self) -> None:
        """Select the previous visible tree item (Shift+Enter in filter bar)."""
        visible = self._visible_tree_items()
        if not visible:
            return
        self._filter_cycle_index = (self._filter_cycle_index - 1) % len(visible)
        self.tree.setCurrentItem(visible[self._filter_cycle_index])
        self.tree.scrollToItem(visible[self._filter_cycle_index])

    def eventFilter(self, obj, event) -> bool:
        """Catch Enter/Shift+Enter on filter bar for match cycling."""
        if obj is self.search_bar and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._filter_prev()
                else:
                    self._filter_next()
                return True
        return super().eventFilter(obj, event)


class WelcomeDialog(QDialog):
    """First-run welcome modal explaining what Scaffold does and where to start."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Scaffold")
        self.setModal(True)
        self.setFixedWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        tools_path = html.escape(str(_tools_dir().resolve()))
        intro = QLabel(
            "<p>Scaffold turns CLI tools into native GUI forms. Each tool is "
            "described by a JSON schema; the form is generated automatically.</p>"
            "<p>To get started:</p>"
            "<ul>"
            "<li>Pick a bundled tool from the picker</li>"
            "<li>Save your current form as a preset for re-use</li>"
            "<li>Chain tools sequentially via the Cascade dock "
            "(<b>Ctrl+G</b> to toggle)</li>"
            "</ul>"
            "<p>Drop your own <code>*.json</code> schemas into:</p>"
            f"<p><code>{tools_path}</code></p>"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(intro)

        self.dont_show_cb = QCheckBox("Don't show again")
        self.dont_show_cb.setChecked(True)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.dont_show_cb)
        btn_row.addStretch()
        ok_btn = QPushButton("Got it")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def accept(self) -> None:
        if self.dont_show_cb.isChecked():
            _create_settings().setValue("picker/welcome_dismissed", 1)
        super().accept()


# ---------------------------------------------------------------------------
# Cascade list picker dialog
# ---------------------------------------------------------------------------

class CascadeListDialog(QDialog):
    """Modal dialog for selecting a saved cascade file, with optional export mode."""

    def __init__(self, mode: str = "load", parent=None):
        super().__init__(parent)
        self.mode = mode  # "load" or "export"
        self.selected_path: str | None = None

        title = "Export Cascade" if mode == "export" else "Cascade List"
        self.setWindowTitle(title)
        self.resize(550, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # Search bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Filter cascades...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self._on_filter)
        layout.addWidget(self.search_bar)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Name", "Description", "Steps", "Modified"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setCascadingSectionResizes(True)
        self.table.horizontalHeader().setMinimumSectionSize(50)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        _wire_last_column(self, self.table)
        _orig_resize = self.table.resizeEvent
        self.table.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self.table))
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        layout.addWidget(self.table, 1)

        # Buttons
        btn_bar = QHBoxLayout()
        if mode == "load":
            self.delete_btn = QPushButton("Delete")
            self.delete_btn.setEnabled(False)
            self.delete_btn.clicked.connect(self._on_delete)
            btn_bar.addWidget(self.delete_btn)
        else:
            self.delete_btn = None
        btn_bar.addStretch()
        action_label = "Export" if mode == "export" else "Load"
        self.action_btn = QPushButton(action_label)
        self.action_btn.setEnabled(False)
        self.action_btn.clicked.connect(self._on_action)
        btn_bar.addWidget(self.action_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_bar.addWidget(cancel_btn)
        layout.addLayout(btn_bar)

        self._populate()

    def _populate(self) -> None:
        """Scan cascades dir and fill the table with valid cascade files."""
        cascade_dir = _cascades_dir()
        cascade_files = sorted(cascade_dir.glob("*.json"))

        valid = []
        for f in cascade_files:
            try:
                data = _read_json_file(f)
                if not isinstance(data, dict):
                    continue
                if data.get("_format") != "scaffold_cascade":
                    continue
                valid.append((f, data))
            except (json.JSONDecodeError, OSError):
                continue

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(valid))
        for row, (f, data) in enumerate(valid):
            # Name
            name_item = QTableWidgetItem(data.get("name", f.stem))
            name_item.setData(Qt.ItemDataRole.UserRole, str(f))
            self.table.setItem(row, 0, name_item)
            # Description
            self.table.setItem(row, 1, QTableWidgetItem(data.get("description", "")))
            # Steps count
            steps = data.get("steps", [])
            count_item = QTableWidgetItem()
            if isinstance(steps, list):
                count_item.setData(Qt.ItemDataRole.DisplayRole, len(steps))
            else:
                count_item.setData(Qt.ItemDataRole.DisplayRole, "0 (malformed)")
            self.table.setItem(row, 2, count_item)
            # Modified date
            try:
                mtime = f.stat().st_mtime
                dt = datetime.datetime.fromtimestamp(mtime)
                date_str = dt.strftime("%b %d, %Y %I:%M %p")
            except OSError:
                date_str = "Unknown"
            self.table.setItem(row, 3, QTableWidgetItem(date_str))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.resizeColumnToContents(0)
        _fit_last_column(self.table)

        self.action_btn.setEnabled(False)
        if self.delete_btn:
            self.delete_btn.setEnabled(False)

    def _on_selection(self) -> None:
        """Enable/disable buttons based on selection."""
        has_sel = len(self.table.selectionModel().selectedRows()) > 0
        self.action_btn.setEnabled(has_sel)
        if self.delete_btn:
            self.delete_btn.setEnabled(has_sel)

    def _path_for_row(self, row: int) -> Path | None:
        """Resolve the cascade file path stored on the column-0 item for *row*."""
        item = self.table.item(row, 0)
        if item is None:
            return None
        stored = item.data(Qt.ItemDataRole.UserRole)
        return Path(stored) if stored else None

    def _on_double_click(self, index) -> None:
        """Accept on double-click."""
        p = self._path_for_row(index.row())
        if p is not None:
            self.selected_path = str(p)
            self.accept()

    def _on_action(self) -> None:
        """Accept with the selected cascade path."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        p = self._path_for_row(rows[0].row())
        if p is not None:
            self.selected_path = str(p)
            self.accept()

    def _on_delete(self) -> None:
        """Delete the selected cascade file."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        cascade_path = self._path_for_row(row)
        if cascade_path is None:
            return
        name = cascade_path.stem

        confirm = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete cascade '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            cascade_path.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Cannot delete cascade:\n{e}")
            return

        self.table.removeRow(row)
        if self.table.rowCount() == 0:
            self.reject()

    def _on_filter(self, text: str) -> None:
        """Filter table rows by search text (name + description)."""
        query = text.strip().lower()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            desc_item = self.table.item(row, 1)
            name_text = name_item.text().lower() if name_item else ""
            desc_text = desc_item.text().lower() if desc_item else ""
            self.table.setRowHidden(row, bool(query) and query not in name_text and query not in desc_text)


# ---------------------------------------------------------------------------
# Cascade variable input dialog
# ---------------------------------------------------------------------------


class CascadeVariableDialog(QDialog):
    """Modal dialog for entering cascade variable values before a chain run."""

    def __init__(self, variables: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cascade Variables")
        self._variables = variables
        self._inputs: dict[str, QWidget] = {}  # flag -> value-bearing widget

        layout = QVBoxLayout(self)

        if not variables:
            layout.addWidget(QLabel("No variables defined."))
        else:
            form = QFormLayout()
            for var in variables:
                name = var.get("name", "")
                flag = var.get("flag", "")
                type_hint = var.get("type_hint", "string")
                description = var.get("description", "")
                row_widget = self._build_row(flag, type_hint, description)
                form.addRow(QLabel(name), row_widget)
            layout.addLayout(form)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # -- row builders -------------------------------------------------------

    def _build_row(self, flag: str, type_hint: str, description: str) -> QWidget:
        """Return the appropriate input widget for *type_hint*."""
        if type_hint == "integer":
            w = QSpinBox()
            w.setRange(-999999, 999999)
            w.setValue(0)
            if description:
                w.setToolTip(description)
            self._inputs[flag] = w
            return w

        if type_hint == "float":
            w = QDoubleSpinBox()
            w.setRange(-999999.0, 999999.0)
            w.setValue(0.0)
            if description:
                w.setToolTip(description)
            self._inputs[flag] = w
            return w

        if type_hint in ("file", "directory"):
            container = QWidget()
            h = QHBoxLayout(container)
            h.setContentsMargins(0, 0, 0, 0)
            le = QLineEdit()
            if description:
                le.setPlaceholderText(description)
            h.addWidget(le)
            btn = QPushButton("Browse...")
            if type_hint == "file":
                btn.clicked.connect(lambda _, edit=le: _browse_file(edit, self))
            else:
                btn.clicked.connect(lambda _, edit=le: _browse_directory(edit, self))
            h.addWidget(btn)
            self._inputs[flag] = le
            return container

        # "string" or unknown — default to QLineEdit
        w = QLineEdit()
        if description:
            w.setPlaceholderText(description)
        self._inputs[flag] = w
        return w

    # -- accept / validate --------------------------------------------------

    def _on_accept(self) -> None:
        has_error = False
        for var in self._variables:
            flag = var.get("flag", "")
            type_hint = var.get("type_hint", "string")
            if type_hint in ("string", "file", "directory"):
                widget = self._inputs.get(flag)
                if widget and isinstance(widget, QLineEdit):
                    if not widget.text().strip():
                        widget.setStyleSheet(_invalid_style())
                        has_error = True
                    else:
                        widget.setStyleSheet("")
        if not has_error:
            self.accept()

    # -- public API ---------------------------------------------------------

    def get_values(self) -> dict:
        """Return ``{flag: value}`` for every variable."""
        result = {}
        for var in self._variables:
            flag = var.get("flag", "")
            type_hint = var.get("type_hint", "string")
            widget = self._inputs.get(flag)
            if widget is None:
                continue
            if type_hint == "integer" and isinstance(widget, QSpinBox):
                result[flag] = widget.value()
            elif type_hint == "float" and isinstance(widget, QDoubleSpinBox):
                result[flag] = widget.value()
            else:
                result[flag] = widget.text()
        return result


# ---------------------------------------------------------------------------
# Cascade variable definition dialog
# ---------------------------------------------------------------------------


_VAR_TYPE_CHOICES = ["string", "file", "directory", "integer", "float"]

_FLAG_COL_MIN = 80
_FLAG_COL_MAX = 280


class ApplyToButton(QToolButton):
    """Dropdown button for selecting which cascade slots a variable applies to."""

    def __init__(self, value: str | list = "all", slot_count: int = 20, parent=None):
        super().__init__(parent)
        self._value: str | list = "all"
        self._slot_count = max(1, slot_count)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._all_action = self._menu.addAction("All")
        self._all_action.setCheckable(True)
        self._none_action = self._menu.addAction("None")
        self._none_action.setCheckable(True)
        self._menu.addSeparator()
        self._slot_actions: list[QAction] = []
        for i in range(self._slot_count):
            act = self._menu.addAction(f"Slot {i + 1}")
            act.setCheckable(True)
            act.triggered.connect(self._on_slot_toggled)
            self._slot_actions.append(act)
        self._all_action.triggered.connect(self._on_all)
        self._none_action.triggered.connect(self._on_none)
        self.set_value(value)

    def _on_all(self) -> None:
        for act in self._slot_actions:
            act.setChecked(False)
        self._none_action.setChecked(False)
        self._all_action.setChecked(True)
        self._value = "all"
        self._update_label()

    def _on_none(self) -> None:
        for act in self._slot_actions:
            act.setChecked(False)
        self._all_action.setChecked(False)
        self._none_action.setChecked(True)
        self._value = "none"
        self._update_label()

    def _on_slot_toggled(self) -> None:
        self._all_action.setChecked(False)
        self._none_action.setChecked(False)
        selected = [i for i, act in enumerate(self._slot_actions) if act.isChecked()]
        if not selected:
            self._all_action.setChecked(True)
            self._value = "all"
        else:
            self._value = selected
        self._update_label()

    def set_value(self, value: str | list) -> None:
        for act in self._slot_actions:
            act.setChecked(False)
        self._all_action.setChecked(False)
        self._none_action.setChecked(False)
        if value == "none":
            self._none_action.setChecked(True)
            self._value = "none"
        elif isinstance(value, list):
            valid = sorted(set(i for i in value if isinstance(i, int) and 0 <= i < self._slot_count))
            if valid:
                for i in valid:
                    self._slot_actions[i].setChecked(True)
                self._value = valid
            else:
                self._all_action.setChecked(True)
                self._value = "all"
        else:
            self._all_action.setChecked(True)
            self._value = "all"
        self._update_label()

    def value(self) -> str | list:
        return self._value

    def _update_label(self) -> None:
        if self._value == "all":
            self.setText("All")
        elif self._value == "none":
            self.setText("None")
        elif isinstance(self._value, list):
            self.setText(", ".join(str(i + 1) for i in self._value))
        else:
            self.setText("All")


class CustomPathDialog(QDialog):
    """Modal dialog for managing custom PATH directories."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom PATH Directories")
        self.setMinimumWidth(500)
        self.setModal(True)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Add directories containing scripts or tools you want Scaffold to find. "
            "These are prepended to your system PATH for binary resolution and execution."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self._list = QListWidget()
        layout.addWidget(self._list)

        # Load raw stored paths (including non-existent) for editing
        s = _create_settings()
        raw = s.value("custom_paths", "")
        self._paths: list[str] = []
        _load_failed = False
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    self._paths = [p for p in loaded if p and isinstance(p, str)]
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                print(
                    f"[scaffold] custom_paths settings unreadable: {exc}",
                    file=sys.stderr,
                )
                _load_failed = True
        if _load_failed:
            _warn_label = QLabel("Previous paths could not be loaded")
            _warn_label.setStyleSheet("color: #c62828;")
            layout.insertWidget(layout.indexOf(self._list), _warn_label)
        self._refresh_list()

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._list.currentRowChanged.connect(
            lambda row: self._remove_btn.setEnabled(row >= 0)
        )

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _refresh_list(self) -> None:
        self._list.clear()
        for p in self._paths:
            item = QListWidgetItem(p)
            if not Path(p).is_dir():
                item.setText(p + "  (not found)")
                if _dark_mode:
                    item.setForeground(QColor(DARK_COLORS["error"]))
                else:
                    item.setForeground(QColor("#c62828"))
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            self._list.addItem(item)

    def _on_add(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Directory")
        if not d:
            return
        if os.pathsep in d:
            QMessageBox.warning(
                self,
                "Invalid Directory",
                f"The selected directory contains '{os.pathsep}', "
                f"which cannot be used in a PATH entry.",
            )
            return
        d = os.path.normpath(d)
        # Duplicate check (case-insensitive on Windows)
        for existing in self._paths:
            if os.path.normcase(existing) == os.path.normcase(d):
                return
        self._paths.append(d)
        self._refresh_list()

    def _on_remove(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._paths):
            self._paths.pop(row)
            self._refresh_list()

    def _on_accept(self) -> None:
        _set_custom_paths(self._paths)
        self.accept()

    def get_paths(self) -> list[str]:
        return list(self._paths)


class CascadeVariableDefinitionDialog(QDialog):
    """Modal dialog for defining cascade variables (name, flag, type, apply-to)."""

    def __init__(self, variables: list[dict], parent=None, slot_count: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Cascade Variables")
        self.setMinimumWidth(560)
        self._result_variables: list[dict] = []

        # Determine slot count from parent CascadeSidebar or fall back
        if slot_count is not None:
            self._slot_count = slot_count
        elif parent is not None and hasattr(parent, "_slots"):
            self._slot_count = len(parent._slots)
        else:
            self._slot_count = CASCADE_MAX_SLOTS

        layout = QVBoxLayout(self)

        # --- table ---
        self._table = QTableWidget(0, 4)
        _headers = [
            ("Name", "Human-readable label shown in the run-time variable prompt "
                     "dialog. Free text; used only for display, not as a key."),
            ("Flag", "The form field key this variable's value gets injected into "
                     "when the chain runs. Must match a flag name in the target "
                     "tool's schema (use subcommand:flag for scoped flags)."),
            ("Type", "Input widget shown in the run-time prompt: string, integer, "
                     "boolean, etc. Controls validation and how the value is "
                     "rendered in the prompt dialog."),
            ("Apply To", "Which slots in the cascade receive this variable's value.\n"
                         "all \u2014 injects into every step\n"
                         "none \u2014 defines the variable but injects nowhere\n"
                         "A comma-separated list of slot numbers limits injection "
                         "to those specific steps only."),
        ]
        for col, (label, tip) in enumerate(_headers):
            item = QTableWidgetItem(label)
            item.setToolTip(tip)
            self._table.setHorizontalHeaderItem(col, item)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(True)
        header.setMinimumSectionSize(50)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 120)
        header.resizeSection(1, 160)
        header.resizeSection(2, 110)
        _wire_last_column(self, self._table, extra=self._enforce_flag_col_bounds)
        _orig_resize = self._table.resizeEvent
        self._table.resizeEvent = lambda e: (_orig_resize(e), _fit_last_column(self._table))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._table)

        # --- add / remove buttons ---
        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Variable")
        add_btn.clicked.connect(self._add_row)
        btn_row.addWidget(add_btn)
        self._remove_btn = QPushButton("Remove Selected")
        self._remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- ok / cancel ---
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Populate from existing variables
        for var in variables:
            self._add_row(
                name=var.get("name", ""),
                flag=var.get("flag", ""),
                type_hint=var.get("type_hint", "string"),
                apply_to=var.get("apply_to", "all"),
            )
        _fit_last_column(self._table)

    # -- row management -----------------------------------------------------

    def _add_row(
        self,
        name: str = "",
        flag: str = "",
        type_hint: str = "string",
        apply_to: str | list = "all",
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        self._table.setItem(row, 0, QTableWidgetItem(name))
        self._table.setItem(row, 1, QTableWidgetItem(flag))

        type_combo = QComboBox()
        type_combo.addItems(_VAR_TYPE_CHOICES)
        idx = _VAR_TYPE_CHOICES.index(type_hint) if type_hint in _VAR_TYPE_CHOICES else 0
        type_combo.setCurrentIndex(idx)
        self._table.setCellWidget(row, 2, type_combo)

        apply_btn = ApplyToButton(value=apply_to, slot_count=self._slot_count)
        self._table.setCellWidget(row, 3, apply_btn)

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._table.removeRow(row)

    def _enforce_flag_col_bounds(self, idx: int, _old: int, new: int) -> None:
        """Clamp the Flag column width to [_FLAG_COL_MIN, _FLAG_COL_MAX]."""
        if idx != 1:
            return
        header = self._table.horizontalHeader()
        if new < _FLAG_COL_MIN:
            header.resizeSection(1, _FLAG_COL_MIN)
        elif new > _FLAG_COL_MAX:
            header.resizeSection(1, _FLAG_COL_MAX)

    # -- accept / validate --------------------------------------------------

    def _on_accept(self) -> None:
        variables: list[dict] = []
        error_lines: list[str] = []
        # Track which rows have valid name+flag for duplicate/format checking
        valid_rows: list[tuple[int, str]] = []  # (row, flag)
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            flag_item = self._table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            flag = flag_item.text().strip() if flag_item else ""
            if not name or not flag:
                if name_item and not name:
                    name_item.setBackground(QColor("#553333"))
                if flag_item and not flag:
                    flag_item.setBackground(QColor("#553333"))
                error_lines.append(f"Row {row + 1}: name and flag must not be empty")
                continue
            # Clear any previous error highlight
            if name_item:
                name_item.setBackground(QColor(0, 0, 0, 0))
            if flag_item:
                flag_item.setBackground(QColor(0, 0, 0, 0))

            # Flag format validation
            if not _CASCADE_VAR_FLAG_RE.match(flag):
                flag_item.setBackground(QColor("#553333"))
                error_lines.append(
                    f'Row {row + 1}: flag "{flag}" is not a valid flag or positional'
                )
                continue

            type_combo = self._table.cellWidget(row, 2)
            type_hint = type_combo.currentText() if type_combo else "string"

            apply_btn = self._table.cellWidget(row, 3)
            apply_to: str | list = apply_btn.value() if isinstance(apply_btn, ApplyToButton) else "all"

            valid_rows.append((row, flag))
            variables.append({
                "name": name,
                "flag": flag,
                "type_hint": type_hint,
                "apply_to": apply_to,
            })

        # Check for duplicate flags among valid rows
        flag_to_rows: dict[str, list[int]] = {}
        for row, flag in valid_rows:
            flag_to_rows.setdefault(flag, []).append(row)
        for flag, rows in flag_to_rows.items():
            if len(rows) > 1:
                for r in rows:
                    flag_item = self._table.item(r, 1)
                    if flag_item:
                        flag_item.setBackground(QColor("#553333"))
                row_nums = " and ".join(str(r + 1) for r in rows)
                error_lines.append(f'Duplicate flag "{flag}" on rows {row_nums}')

        if error_lines:
            QMessageBox.warning(self, "Invalid Variables", "\n".join(error_lines))
            return
        self._result_variables = variables
        self.accept()

    # -- public API ---------------------------------------------------------

    def get_variables(self) -> list[dict]:
        """Return the list of variable definitions."""
        return list(self._result_variables)


class CascadeCaptureDefinitionDialog(QDialog):
    """Modal dialog for defining captures for a single cascade slot."""

    _SOURCES = ["stdout", "stderr", "file", "exit_code", "stdout_tail", "stderr_tail"]

    def __init__(
        self,
        captures: list[dict],
        cascade_variables: list[dict] | None = None,
        slot_number: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Captures \u2014 Step {slot_number}")
        self.setMinimumWidth(560)
        self._cascade_variables = cascade_variables if cascade_variables is not None else []
        self._result_captures: list[dict] = []

        layout = QVBoxLayout(self)

        # --- table ---
        self._table = QTableWidget(0, 4)
        _headers = [
            ("Name", "Identifier for this capture. Letters, digits, and underscores only; "
                     "must start with a letter or underscore. Used in later steps as "
                     "{name} to substitute the captured value into form fields."),
            ("Source", "Where the captured value comes from:\n"
                       "\u2022 stdout / stderr \u2014 regex match against the step's output stream "
                       "(last 64KB)\n"
                       "\u2022 file \u2014 a literal filesystem path, passed through as-is\n"
                       "\u2022 exit_code \u2014 the step's exit code as a string\n"
                       "\u2022 stdout_tail / stderr_tail \u2014 last 64KB of the output stream, "
                       "no regex needed"),
            ("Pattern/Path", "For stdout/stderr sources: a regular expression "
                             "(max 200 characters). The capture group specified in the "
                             "Group column is extracted on match.\n"
                             "For file source: the literal path string to pass to later steps.\n"
                             "Disabled for exit_code and stdout_tail/stderr_tail sources."),
            ("Group", "Regex capture group index to extract (1 = first parenthesized group). "
                      "Only used when source is stdout or stderr. Defaults to 1."),
        ]
        for col, (label, tip) in enumerate(_headers):
            item = QTableWidgetItem(label)
            item.setToolTip(tip)
            self._table.setHorizontalHeaderItem(col, item)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._table)

        # --- add / remove buttons ---
        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Capture")
        add_btn.clicked.connect(self._add_row)
        btn_row.addWidget(add_btn)
        self._remove_btn = QPushButton("Remove Selected")
        self._remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- ok / cancel ---
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Populate from existing captures
        for cap in captures:
            source = cap.get("source", "stdout")
            if source in ("stdout", "stderr"):
                self._add_row(
                    name=cap.get("name", ""),
                    source=source,
                    pattern_or_path=cap.get("pattern", ""),
                    group=cap.get("group", 1),
                )
            elif source == "file":
                self._add_row(
                    name=cap.get("name", ""),
                    source=source,
                    pattern_or_path=cap.get("path", ""),
                )
            else:
                self._add_row(
                    name=cap.get("name", ""),
                    source=source,
                )

    # -- row management -----------------------------------------------------

    def _add_row(
        self,
        name: str = "",
        source: str = "stdout",
        pattern_or_path: str = "",
        group: int = 1,
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        self._table.setItem(row, 0, QTableWidgetItem(name))

        source_combo = QComboBox()
        source_combo.addItems(self._SOURCES)
        idx = self._SOURCES.index(source) if source in self._SOURCES else 0
        source_combo.setCurrentIndex(idx)
        self._table.setCellWidget(row, 1, source_combo)

        self._table.setItem(row, 2, QTableWidgetItem(pattern_or_path))
        self._table.setItem(row, 3, QTableWidgetItem(str(group)))

        self._update_row_enablement(row)
        source_combo.currentTextChanged.connect(self._source_combo_changed)

    def _source_combo_changed(self, text: str) -> None:
        combo = self.sender()
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, 1) is combo:
                self._update_row_enablement(row)
                break

    def _update_row_enablement(self, row: int) -> None:
        combo = self._table.cellWidget(row, 1)
        if not combo:
            return
        source = combo.currentText()
        pat_item = self._table.item(row, 2)
        grp_item = self._table.item(row, 3)
        if not pat_item or not grp_item:
            return

        dim_bg = QColor("#333333") if _dark_mode else QColor("#e0e0e0")
        clear_bg = QColor(0, 0, 0, 0)

        if source in ("stdout", "stderr"):
            pat_item.setFlags(pat_item.flags() | Qt.ItemFlag.ItemIsEditable)
            pat_item.setBackground(clear_bg)
            grp_item.setFlags(grp_item.flags() | Qt.ItemFlag.ItemIsEditable)
            grp_item.setBackground(clear_bg)
        elif source == "file":
            pat_item.setFlags(pat_item.flags() | Qt.ItemFlag.ItemIsEditable)
            pat_item.setBackground(clear_bg)
            grp_item.setFlags(grp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            grp_item.setBackground(dim_bg)
        else:  # exit_code, stdout_tail, stderr_tail
            pat_item.setFlags(pat_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pat_item.setBackground(dim_bg)
            grp_item.setFlags(grp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            grp_item.setBackground(dim_bg)

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._table.removeRow(row)

    # -- accept / validate --------------------------------------------------

    def _on_accept(self) -> None:
        entries: list[dict] = []
        error_lines: list[str] = []
        error_rows: set[int] = set()
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            name = name_item.text().strip() if name_item else ""

            combo = self._table.cellWidget(row, 1)
            source = combo.currentText() if combo else "stdout"

            pat_item = self._table.item(row, 2)
            pat_text = pat_item.text().strip() if pat_item else ""

            grp_item = self._table.item(row, 3)
            grp_text = grp_item.text().strip() if grp_item else "1"

            # Build entry dict based on source
            if source in ("stdout", "stderr"):
                try:
                    group_val = int(grp_text)
                except ValueError:
                    error_rows.add(row)
                    if grp_item:
                        grp_item.setBackground(QColor("#553333"))
                    row_label = f'Row {row + 1} (name="{name}")' if name else f"Row {row + 1}"
                    error_lines.append(f"{row_label}: group must be an integer")
                    continue
                entry = {"name": name, "source": source, "pattern": pat_text, "group": group_val}
            elif source == "file":
                entry = {"name": name, "source": "file", "path": pat_text}
            else:
                entry = {"name": name, "source": source}

            # Validate via schema validator
            try:
                _validate_capture_entry(entry, self._cascade_variables)
            except ValueError as exc:
                error_rows.add(row)
                if name_item:
                    name_item.setBackground(QColor("#553333"))
                row_label = f'Row {row + 1} (name="{name}")' if name else f"Row {row + 1}"
                error_lines.append(f"{row_label}: {exc}")
                continue

            # Clear highlights on success
            if name_item:
                name_item.setBackground(QColor(0, 0, 0, 0))
            if grp_item:
                grp_item.setBackground(QColor(0, 0, 0, 0))

            entries.append(entry)

        if error_lines:
            QMessageBox.warning(self, "Invalid Capture", "\n".join(error_lines))
            return
        self._result_captures = entries
        self.accept()

    # -- public API ---------------------------------------------------------

    def get_captures(self) -> list[dict]:
        """Return the list of capture definitions."""
        return list(self._result_captures)


# ---------------------------------------------------------------------------
# Cascade sidebar dock widget
# ---------------------------------------------------------------------------

CASCADE_MAX_SLOTS = 20
CASCADE_INITIAL_SLOTS = 2

# Chain runner states
CHAIN_IDLE = "idle"
CHAIN_LOADING = "loading"       # Loading tool + preset into form
CHAIN_RUNNING = "running"       # QProcess is executing
CHAIN_PAUSED = "paused"         # Chain paused between steps

_CAPTURE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPTURE_VALID_SOURCES = frozenset({"stdout", "stderr", "file", "exit_code", "stdout_tail", "stderr_tail"})


def _pattern_is_redos_prone(pattern: str) -> tuple[bool, str]:
    """Detect common ReDoS-prone regex shapes.

    Returns (True, reason) for patterns with nested quantifiers or
    alternation-under-quantifier, else (False, "").

    This is a heuristic, not a proof. It catches classical ReDoS
    forms with zero false positives on realistic capture patterns,
    but cannot detect all pathological regex shapes. Paired with
    runtime bounded execution in extract_captures.
    """
    if not isinstance(pattern, str):
        return (False, "")
    if _REDOS_NESTED_QUANT_RE.search(pattern):
        return (True, "nested quantifier (catastrophic backtracking risk)")
    if _REDOS_ALT_QUANT_RE.search(pattern):
        return (True, "alternation under quantifier (catastrophic backtracking risk)")
    return (False, "")


def _regex_worker_entry(pattern: str, stream: str, group: int) -> tuple[bool, str, str]:
    """Worker entry for bounded regex execution.

    Returns (success, result_or_error, error_type):
      (True, matched_text, "")          on successful match
      (True, "", "no_match")            on successful no-match
      (False, error_msg, "re_error")    on compile/runtime error
    """
    import re as _re  # local import — worker process
    try:
        m = _re.search(pattern, stream)
    except _re.error as exc:
        return (False, str(exc), "re_error")
    if m is None:
        return (True, "", "no_match")
    try:
        return (True, m.group(group), "")
    except IndexError as exc:
        return (False, str(exc), "re_error")


_regex_pool = None
_regex_pool_lock = threading.Lock()


def _get_regex_pool():
    """Lazy single-worker multiprocessing.Pool for bounded regex execution.

    Created on first use. Reused across cascade steps to amortize
    Windows spawn cost (~80ms). Replaced after a timeout because
    pool.terminate() kills the worker and leaves the pool unusable.
    """
    global _regex_pool
    with _regex_pool_lock:
        if _regex_pool is None:
            # Windows-spawn safety: if the parent script has no
            # `if __name__ == "__main__":` guard (common in test harnesses
            # and ad-hoc scripts), the spawned child would runpy the
            # entire parent script as __mp_main__, re-executing all
            # top-level code. We neutralise this by temporarily hiding
            # __main__.__file__ so multiprocessing skips the main-module
            # re-import; the child still unpickles scaffold._regex_worker_entry
            # via a normal `import scaffold` lookup.
            main_mod = sys.modules.get("__main__")
            saved_file = None
            had_file = False
            if main_mod is not None:
                had_file = hasattr(main_mod, "__file__")
                if had_file:
                    saved_file = main_mod.__file__
                    try:
                        main_mod.__file__ = None
                    except (AttributeError, RuntimeError):
                        had_file = False
            try:
                try:
                    _regex_pool = multiprocessing.Pool(processes=1)
                except (OSError, RuntimeError, ValueError) as _pool_exc:
                    print(
                        f"[scaffold] regex pool unavailable: {_pool_exc}",
                        file=sys.stderr,
                    )
                    _regex_pool = None
            finally:
                if had_file and main_mod is not None:
                    try:
                        main_mod.__file__ = saved_file
                    except (AttributeError, RuntimeError):
                        pass
        return _regex_pool


def _reset_regex_pool():
    """Terminate and discard the current pool. Next access recreates it."""
    global _regex_pool
    with _regex_pool_lock:
        if _regex_pool is not None:
            try:
                _regex_pool.terminate()
                _regex_pool.join()
            except (AttributeError, OSError, RuntimeError):
                pass
            finally:
                _regex_pool = None


def _bounded_regex_search(pattern: str, stream: str, group: int,
                          timeout: float = MAX_REGEX_SECONDS
                          ) -> tuple[bool, str, str]:
    """Run re.search in a worker process with a wall-clock timeout.

    Returns the same (success, result_or_error, error_type) shape as
    _regex_worker_entry, plus one new error_type: "timeout".

    On timeout, terminates the worker (via _reset_regex_pool) and
    returns (False, message, "timeout").

    Worst-case UI block: ~2s per regex on a cascade step that hits a
    pathological pattern. The block is IPC-bound (pipe read releases
    the GIL), not CPU-bound. A fully-async QTimer-polled design would
    eliminate it; deferred as future work.
    """
    pool = _get_regex_pool()
    if pool is None:
        return (
            False,
            "regex pool unavailable (worker process could not start)",
            "pool_error",
        )
    async_result = pool.apply_async(
        _regex_worker_entry, (pattern, stream, group)
    )
    try:
        return async_result.get(timeout=timeout)
    except multiprocessing.TimeoutError:
        _reset_regex_pool()
        return (
            False,
            f"timeout: regex exceeded {timeout:.1f}s",
            "timeout",
        )
    # broadly defensive — pool.get() re-raises arbitrary worker exceptions; narrowing would turn unexpected worker errors into uncaught escapes.
    except Exception as exc:
        _reset_regex_pool()
        return (False, f"worker error: {exc}", "worker_error")


def _validate_capture_entry(entry: dict, cascade_variables: list) -> None:
    """Validate a single capture entry dict. Raises ValueError on any violation."""
    if not isinstance(entry, dict):
        raise ValueError("capture entry must be a dict")
    # Name validation
    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("capture entry must have a non-empty 'name' string")
    if not _CAPTURE_NAME_RE.match(name):
        raise ValueError(f"capture '{name}': name must match [A-Za-z_][A-Za-z0-9_]*")
    if name in CAPTURE_RESERVED_NAMES:
        raise ValueError(f"capture '{name}': name is reserved")
    for var in cascade_variables:
        if isinstance(var, dict) and var.get("name") == name:
            raise ValueError(f"capture '{name}': name collides with cascade variable")
    # Source validation
    source = entry.get("source")
    if source not in _CAPTURE_VALID_SOURCES:
        raise ValueError(f"capture '{name}': source must be one of {sorted(_CAPTURE_VALID_SOURCES)}")
    # Source-specific validation
    if source in ("stdout", "stderr"):
        pattern = entry.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ValueError(f"capture '{name}': pattern required for source '{source}'")
        if len(pattern) > 200:
            raise ValueError(f"capture '{name}': pattern exceeds 200 chars")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"capture '{name}': invalid regex pattern: {exc}"
            ) from exc
        is_prone, reason = _pattern_is_redos_prone(pattern)
        if is_prone:
            raise ValueError(
                f"capture '{name}': regex has {reason}"
            )
        group = entry.get("group")
        if group is not None and (not isinstance(group, int) or group < 0):
            raise ValueError(f"capture '{name}': group must be int >= 0")
    elif source == "file":
        path = entry.get("path")
        if not path or not isinstance(path, str):
            raise ValueError(f"capture '{name}': path required for source 'file'")


def extract_captures(
    slot_captures: list[dict],
    stdout: str,
    stderr: str,
    exit_code: int,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Extract capture values from command output. Pure function — no side effects.

    Returns (values, errors) where values maps capture name to extracted
    string and errors is a list of (name, error_message, err_type) tuples
    for captures whose regex failed. err_type is one of: "re_error",
    "timeout", "worker_error", "pool_error".
    """
    result: dict[str, str] = {}
    errors: list[tuple[str, str, str]] = []
    stdout_tail = stdout[-CAPTURE_STREAM_TAIL_BYTES:]
    stderr_tail = stderr[-CAPTURE_STREAM_TAIL_BYTES:]
    for entry in slot_captures:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or not isinstance(name, str):
            continue
        source = entry.get("source")
        if source in ("stdout", "stderr"):
            pattern = entry.get("pattern")
            if not pattern or not isinstance(pattern, str):
                continue
            stream = stdout_tail if source == "stdout" else stderr_tail
            group = entry.get("group", 1)
            success, value, err_type = _bounded_regex_search(pattern, stream, group)
            if not success:
                errors.append((name, value, err_type))
                continue
            if err_type == "no_match":
                continue
            result[name] = value
        elif source == "file":
            # Returns the literal path string from the capture definition,
            # not the contents of the file at that path. Intentional: file
            # captures are designed to hand a filename produced by one tool
            # (e.g. nmap -oX out.xml) to a downstream tool as an input arg.
            path_val = entry.get("path")
            if path_val and isinstance(path_val, str):
                result[name] = path_val
        elif source == "exit_code":
            result[name] = str(exit_code)
        elif source == "stdout_tail":
            result[name] = stdout_tail
        elif source == "stderr_tail":
            result[name] = stderr_tail
    return result, errors


def substitute_captures(text: str, values: dict) -> tuple[str, list[str]]:
    """Replace {name} tokens in text with values[name].

    Returns (substituted_text, unset_names) where unset_names is the list
    of {name} tokens whose name was not in values. Unset tokens are
    replaced with empty string.

    Only substitutes tokens whose inner content matches the capture-name
    charset ^[A-Za-z_][A-Za-z0-9_]*$. Other {...} content (JSON, format
    strings with indices, literal braces) is left untouched.
    """
    if not text:
        return ("", [])
    unset_names: list[str] = []
    seen: set[str] = set()

    def _replacer(m: re.Match) -> str:
        name = m.group(1)
        if name in values:
            return str(values[name])
        if name not in seen:
            seen.add(name)
            unset_names.append(name)
        return ""

    result = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _replacer, text)
    return (result, unset_names)


_ARGV_FLAG_RE = re.compile(r"^--?[A-Za-z]")

# Flag-format regex for cascade variable definitions.
# Accepts:  -f  --flag  --output-file  TARGET  HOST
#           clone:--depth  clone:REPOSITORY  compose up:--env
_CASCADE_VAR_FLAG_RE = re.compile(
    r"^(?:[a-z][a-z0-9_-]*(?:\s[a-z][a-z0-9_-]*)?:)?"  # optional subcommand: prefix (max 2 levels)
    r"(?:--?[A-Za-z][A-Za-z0-9_-]*|[A-Z][A-Z0-9_]*)$"  # flag or POSITIONAL
)


def _looks_like_argv_flag(value: str) -> bool:
    """True if value looks like an argv flag (-x, --flag) but not bare -, --, or numbers."""
    return bool(_ARGV_FLAG_RE.match(value))


class CascadeSidebar(QDockWidget):
    """Collapsible right-side dock with dynamic cascade step slots."""

    def __init__(self, main_window, parent=None):
        super().__init__("Cascade", parent or main_window)
        self._main_window = main_window
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)

        # Data model starts empty; _load_cascade populates it
        self._slots: list[dict] = []
        self._slot_widgets: list[dict] = []
        self._slot_buttons: list[QPushButton] = []
        self._arrow_buttons: list[QPushButton] = []
        self._cascade_variables: list[dict] = []

        # Content widget
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.setSpacing(4)

        # Menu button row (right-aligned)
        menu_row = QHBoxLayout()
        menu_row.addStretch()

        self._menu_btn = QToolButton()
        self._menu_btn.setText("\u2630")
        self._menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu_btn.setAutoRaise(True)
        self._menu_btn.setFixedSize(24, 24)
        self._menu_btn.setStyleSheet(
            "QToolButton::menu-indicator { image: none; width: 0; height: 0; }"
        )
        panel_menu = QMenu(self._menu_btn)
        panel_menu.addAction("Save Cascade...", self._on_save_cascade_file)
        panel_menu.addAction("Cascade List...", self._on_load_cascade_list)
        panel_menu.addSeparator()
        panel_menu.addAction("Import Cascade...", self._on_import_cascade)
        panel_menu.addAction("Export Cascade...", self._on_export_cascade)
        panel_menu.addSeparator()
        panel_menu.addAction("Edit Variables...", self._on_edit_variables)
        self._menu_btn.setMenu(panel_menu)
        menu_row.addWidget(self._menu_btn)

        content_layout.addLayout(menu_row)

        # Scroll area for slots
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_widget = QWidget()
        self._slots_container = QVBoxLayout(scroll_widget)
        self._slots_container.setContentsMargins(0, 0, 0, 0)
        self._slots_container.setSpacing(2)

        # + Add Step button
        self.add_step_btn = QPushButton("+ Add Step")
        self.add_step_btn.clicked.connect(self._on_add_slot)
        self._slots_container.addWidget(self.add_step_btn)

        self._slots_container.addStretch()
        scroll_area.setWidget(scroll_widget)
        scroll_area.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        content_layout.addWidget(scroll_area)

        # Chain controls — two rows to fit within 208px width
        chain_row1 = QHBoxLayout()
        chain_row1.setSpacing(4)
        chain_row2 = QHBoxLayout()
        chain_row2.setSpacing(4)

        self._loop_enabled = False
        self.loop_btn = QPushButton("Loop")
        self.loop_btn.setFixedWidth(65)
        self.loop_btn.setToolTip("<p>Toggle loop mode (re-run continuously)</p>")
        self.loop_btn.clicked.connect(self._toggle_loop)
        self._stop_on_error = False
        self.stop_on_error_btn = QPushButton("Err\u2717")
        self.stop_on_error_btn.setFixedWidth(65)
        self.stop_on_error_btn.setToolTip("<p>Toggle stop-on-error (halt cascade if any step fails)</p>")
        self.stop_on_error_btn.clicked.connect(self._toggle_stop_on_error)
        self.run_chain_btn = QPushButton("Run")
        self.run_chain_btn.setToolTip("<p>Execute the cascade</p>")
        self.pause_chain_btn = QPushButton("Pause")
        self.pause_chain_btn.setToolTip(
            "<p>Pause execution</p>"
        )
        self.stop_chain_btn = QPushButton("Stop")
        self.stop_chain_btn.setToolTip("<p>Stop and reset the cascade</p>")
        self.clear_all_btn = QPushButton("Clear")
        self.clear_all_btn.setToolTip("<p>Clear output</p>")
        self.stop_chain_btn.setEnabled(False)
        self.pause_chain_btn.setEnabled(False)
        # Compact padding prevents "Running..." text clipping in light mode
        # where native button chrome is wider than dark mode's QSS padding.
        # _chain_btn_body is the single source for the padding literal; the
        # accented ON-state variants in _style_loop_btn/_style_stop_on_error_btn
        # reference it too.
        _border = DARK_COLORS["border"] if _dark_mode else "#c0c0c0"
        self._chain_btn_body = "padding: 2px 8px; border-radius: 3px;"
        self._chain_qss = f"QPushButton {{ border: 1px solid {_border}; {self._chain_btn_body} }}"
        for _cbtn in (self.run_chain_btn, self.pause_chain_btn, self.stop_chain_btn,
                      self.loop_btn, self.stop_on_error_btn, self.clear_all_btn,
                      self.add_step_btn):
            _cbtn.setStyleSheet(self._chain_qss)
        self.run_chain_btn.clicked.connect(self._on_run_chain)
        self.pause_chain_btn.clicked.connect(self._on_pause_chain)
        self.stop_chain_btn.clicked.connect(self._on_stop_chain)
        self.clear_all_btn.clicked.connect(self._on_clear_all_slots)

        chain_row1.addWidget(self.run_chain_btn, 1)
        chain_row1.addWidget(self.pause_chain_btn, 1)
        chain_row1.addWidget(self.stop_chain_btn, 1)

        chain_row2.addWidget(self.loop_btn)
        chain_row2.addWidget(self.stop_on_error_btn)
        chain_row2.addWidget(self.clear_all_btn, 1)

        chain_controls = QVBoxLayout()
        chain_controls.setSpacing(4)
        chain_controls.addLayout(chain_row1)
        chain_controls.addLayout(chain_row2)
        content_layout.addLayout(chain_controls)

        self._loaded_label = QLabel("")
        self._loaded_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loaded_label.setStyleSheet(
            "color: gray; font-size: 10px; padding: 2px;"
        )
        self._loaded_label.setWordWrap(True)
        content_layout.addWidget(self._loaded_label)

        content.setFixedWidth(220)
        self.setWidget(content)

        # Chain state
        self._chain_state = CHAIN_IDLE
        self._chain_queue: list[int] = []
        self._chain_current = -1
        self._chain_loop_count = 0
        self._stored_original_on_finished = None
        self._chain_variable_values: dict = {}
        self._argv_warned_captures: set[str] = set()
        self._unset_warned_captures: set[str] = set()
        self._chain_paused = False
        # Cascade history tracking
        self._cascade_run_id: str | None = None
        self._cascade_steps: list[dict] = []
        self._cascade_step_start: float | None = None
        self._cascade_finish_status: str | None = None
        self._cascade_run_loop_index: int = 0
        self._current_cascade_name: str | None = None

        # Load saved state (populates self._slots and builds widgets)
        self._load_cascade()
        self._refresh_button_labels()
        self._update_add_button_state()
        self._update_remove_button_state()
        self._update_loaded_label()

    # ------------------------------------------------------------------
    # Cascade name label
    # ------------------------------------------------------------------

    def _update_loaded_label(self) -> None:
        """Refresh the 'Loaded: <name>' label below the cascade controls."""
        if self._current_cascade_name:
            self._loaded_label.setText(f"Loaded: {self._current_cascade_name}")
        else:
            self._loaded_label.setText("Loaded: (unsaved)")

    # ------------------------------------------------------------------
    # Slot widget management
    # ------------------------------------------------------------------

    def _add_slot_widget(self, index: int) -> None:
        """Create and insert a slot widget at the given position."""
        slot_widget = QWidget()
        slot_layout = QVBoxLayout(slot_widget)
        slot_layout.setContentsMargins(0, 0, 0, 2)
        slot_layout.setSpacing(2)

        # Row 1: number label + main button + arrow button + remove button
        row1 = QHBoxLayout()
        row1.setSpacing(2)

        num_label = QLabel(f"{index + 1}")
        num_label.setFixedWidth(16)
        num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        main_btn = QPushButton("(empty)")
        main_btn.setFixedHeight(28)
        main_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        main_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        main_btn.clicked.connect(lambda checked=False, b=main_btn: self._on_slot_clicked(self._slot_buttons.index(b)))
        main_btn.customContextMenuRequested.connect(lambda pos, b=main_btn: self._on_slot_right_click(self._slot_buttons.index(b)))

        arrow_btn = QPushButton("\u25be")
        arrow_btn.setFixedWidth(24)
        arrow_btn.setFixedHeight(28)
        arrow_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        arrow_btn.setToolTip("Change or clear this step")
        arrow_btn.clicked.connect(lambda checked=False, b=arrow_btn: self._on_slot_right_click(self._arrow_buttons.index(b)))
        if _dark_mode:
            arrow_btn.setStyleSheet(f"color: {DARK_COLORS['text_dim']}; padding: 0px;")

        remove_btn = QPushButton("\u2212")
        remove_btn.setFixedWidth(24)
        remove_btn.setFixedHeight(28)
        remove_btn.setToolTip("Remove this step")
        remove_btn.clicked.connect(lambda checked=False, b=remove_btn: self._on_remove_slot(self._find_slot_by_remove_btn(b)))
        if _dark_mode:
            remove_btn.setStyleSheet(f"color: {DARK_COLORS['text_dim']}; padding: 0px;")

        row1.addWidget(num_label)
        row1.addWidget(main_btn, 1)
        row1.addWidget(arrow_btn)
        row1.addWidget(remove_btn)
        slot_layout.addLayout(row1)

        # Row 2: delay label + spinner (centered)
        delay_row = QHBoxLayout()
        delay_row.addStretch()
        dim_color = DARK_COLORS["text_dim"] if _dark_mode else "#666666"
        delay_label = QLabel("Delay")
        delay_label.setStyleSheet(f"font-size: 11px; color: {dim_color};")
        delay_row.addWidget(delay_label)
        delay_spin = QSpinBox()
        delay_spin.setRange(0, 3600)
        saved_delay = self._slots[index].get("delay", 0) if index < len(self._slots) else 0
        delay_spin.setValue(saved_delay)
        delay_spin.setSuffix("s")
        delay_spin.setSingleStep(1)
        delay_spin.setFixedWidth(80)
        delay_spin.setToolTip("Delay after this step (seconds). 0 = no delay.")
        delay_spin.valueChanged.connect(lambda val, b=main_btn: self._on_delay_changed(self._slot_buttons.index(b), val))
        delay_row.addWidget(delay_spin)
        captures_btn = QPushButton("Captures...")
        captures_btn.setFixedHeight(22)
        captures_btn.setToolTip("Define values to capture from this step's output for use in later steps.")
        captures_btn.clicked.connect(
            lambda checked=False, b=captures_btn: self._on_edit_captures(self._find_slot_by_captures_btn(b))
        )
        delay_row.addWidget(captures_btn)
        delay_row.addStretch()
        slot_layout.addLayout(delay_row)

        # Insert into container (before the + button and stretch)
        insert_pos = len(self._slot_widgets)
        self._slots_container.insertWidget(insert_pos, slot_widget)

        # Store widget info
        info = {
            "widget": slot_widget,
            "main_btn": main_btn,
            "arrow_btn": arrow_btn,
            "remove_btn": remove_btn,
            "num_label": num_label,
            "delay_label": delay_label,
            "delay_spin": delay_spin,
            "captures_btn": captures_btn,
        }
        self._slot_widgets.append(info)
        self._slot_buttons.append(main_btn)
        self._arrow_buttons.append(arrow_btn)

    def _find_slot_by_remove_btn(self, btn: QPushButton) -> int:
        """Find slot index by its remove button."""
        for i, info in enumerate(self._slot_widgets):
            if info["remove_btn"] is btn:
                return i
        return -1

    def _find_slot_by_captures_btn(self, btn: QPushButton) -> int:
        """Find slot index by its captures button."""
        for i, info in enumerate(self._slot_widgets):
            if info["captures_btn"] is btn:
                return i
        return -1

    def _new_slot(self) -> dict:
        """Canonical empty slot init."""
        return {"tool_path": None, "preset_path": None, "delay": 0, "captures": []}

    def _on_add_slot(self) -> None:
        """Add a new empty slot at the end."""
        if len(self._slots) >= CASCADE_MAX_SLOTS:
            self._main_window.statusBar().showMessage(f"Maximum {CASCADE_MAX_SLOTS} steps")
            return
        self._slots.append(self._new_slot())
        self._add_slot_widget(len(self._slots) - 1)
        self._save_cascade()
        self._update_add_button_state()
        self._update_remove_button_state()

    def _on_remove_slot(self, index: int) -> None:
        """Remove the slot at the given index."""
        if index < 0 or len(self._slots) <= 1:
            return
        self._slots.pop(index)
        info = self._slot_widgets.pop(index)
        self._slot_buttons.pop(index)
        self._arrow_buttons.pop(index)
        # Hide + reparent before deleteLater to prevent ghost widgets on
        # Linux where deleteLater timing differs
        w = info["widget"]
        w.hide()
        w.setParent(None)
        w.deleteLater()
        self._renumber_slots()
        self._save_cascade()
        self._update_add_button_state()
        self._update_remove_button_state()

    def _renumber_slots(self) -> None:
        """Update all number labels after add/remove."""
        for i, info in enumerate(self._slot_widgets):
            info["num_label"].setText(f"{i + 1}")

    def _update_add_button_state(self) -> None:
        """Disable + button when at max slots."""
        self.add_step_btn.setEnabled(len(self._slots) < CASCADE_MAX_SLOTS)

    def _update_remove_button_state(self) -> None:
        """Disable remove button when only 1 slot remains."""
        only_one = len(self._slot_widgets) <= 1
        for info in self._slot_widgets:
            info["remove_btn"].setEnabled(not only_one)

    def _on_delay_changed(self, index: int, value: int) -> None:
        """Update delay value in the data model when spinner changes."""
        if 0 <= index < len(self._slots):
            self._slots[index]["delay"] = value
            self._save_cascade()

    def _toggle_loop(self) -> None:
        """Toggle loop mode on/off."""
        self._loop_enabled = not self._loop_enabled
        self._style_loop_btn()
        self._save_cascade()

    def _style_loop_btn(self) -> None:
        """Update loop button appearance based on current state."""
        if self._loop_enabled:
            color = DARK_COLORS["success"] if _dark_mode else "#4CAF50"
            self.loop_btn.setStyleSheet(f"QPushButton {{ border: 2px solid {color}; {self._chain_btn_body} }}")
            self.loop_btn.setText("Loop \u2713")
        else:
            self.loop_btn.setStyleSheet(self._chain_qss)
            self.loop_btn.setText("Loop \u2717")

    def _toggle_stop_on_error(self) -> None:
        """Toggle stop-on-error mode on/off."""
        self._stop_on_error = not self._stop_on_error
        self._style_stop_on_error_btn()
        self._save_cascade()

    def _style_stop_on_error_btn(self) -> None:
        """Update stop-on-error button appearance based on current state."""
        if self._stop_on_error:
            color = DARK_COLORS["success"] if _dark_mode else "#4CAF50"
            self.stop_on_error_btn.setStyleSheet(f"QPushButton {{ border: 2px solid {color}; {self._chain_btn_body} }}")
            self.stop_on_error_btn.setText("Err\u2713")
        else:
            self.stop_on_error_btn.setStyleSheet(self._chain_qss)
            self.stop_on_error_btn.setText("Err\u2717")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_cascade(self) -> None:
        """Persist slot data to QSettings."""
        settings = self._main_window.settings
        data = []
        for slot in self._slots:
            data.append({
                "tool_path": slot.get("tool_path"),
                "preset_path": slot.get("preset_path"),
                "delay": slot.get("delay", 0),
                "captures": slot.get("captures", []),
            })
        settings.setValue("cascade/slots", json.dumps(data))
        settings.setValue("cascade/loop_mode", 1 if self._loop_enabled else 0)
        settings.setValue("cascade/stop_on_error", 1 if self._stop_on_error else 0)
        settings.setValue("cascade/variables", json.dumps(self._cascade_variables))
        settings.setValue("cascade/current_name", self._current_cascade_name or "")

    def _load_cascade(self) -> None:
        """Restore slot data from QSettings, migrating from old keys if needed."""
        settings = self._main_window.settings
        raw = settings.value("cascade/slots")
        if raw is None:
            # Migration from old "favorites" keys
            raw = settings.value("favorites/slots")
            if raw is not None:
                settings.setValue("cascade/slots", raw)
                settings.remove("favorites/slots")
            vis = settings.value("favorites/visible")
            if vis is not None:
                settings.setValue("cascade/visible", vis)
                settings.remove("favorites/visible")

        loaded_slots = None
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    loaded_slots = []
                    for item in data:
                        loaded_slots.append({
                            "tool_path": item.get("tool_path") if isinstance(item, dict) else None,
                            "preset_path": item.get("preset_path") if isinstance(item, dict) else None,
                            "delay": item.get("delay", 0) if isinstance(item, dict) else 0,
                            "captures": item.get("captures", []) if isinstance(item, dict) else [],
                        })
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                print(
                    f"[scaffold] cascade slots settings unreadable: {exc}",
                    file=sys.stderr,
                )
                if hasattr(self, "_main_window") and self._main_window is not None:
                    self._main_window.statusBar().showMessage(
                        "Cascade slots could not be loaded \u2014 resetting to empty",
                        5000,
                    )
                loaded_slots = None

        # Migrate renamed capture sources (stdout_full → stdout_tail, etc.)
        _SOURCE_MIGRATION = {"stdout_full": "stdout_tail", "stderr_full": "stderr_tail"}
        if loaded_slots:
            for slot in loaded_slots:
                for cap in slot.get("captures", []):
                    if isinstance(cap, dict) and cap.get("source") in _SOURCE_MIGRATION:
                        cap["source"] = _SOURCE_MIGRATION[cap["source"]]

        if loaded_slots is None:
            loaded_slots = [self._new_slot()
                            for _ in range(CASCADE_INITIAL_SLOTS)]

        # Tear down any existing widgets (hide + detach synchronously to
        # prevent ghost widgets on Linux where deleteLater timing differs)
        for info in self._slot_widgets:
            info["widget"].hide()
            info["widget"].setParent(None)
            info["widget"].deleteLater()
        self._slot_widgets.clear()
        self._slot_buttons.clear()
        self._arrow_buttons.clear()

        self._slots = loaded_slots
        for i in range(len(self._slots)):
            self._add_slot_widget(i)

        # Restore loop mode
        self._loop_enabled = int(self._main_window.settings.value("cascade/loop_mode", 0) or 0) == 1
        self._style_loop_btn()

        # Restore stop-on-error mode
        self._stop_on_error = int(self._main_window.settings.value("cascade/stop_on_error", 0) or 0) == 1
        self._style_stop_on_error_btn()

        # Restore cascade variables
        raw_vars = settings.value("cascade/variables")
        if raw_vars:
            try:
                loaded_vars = json.loads(raw_vars)
                if isinstance(loaded_vars, list):
                    self._cascade_variables = loaded_vars
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                print(
                    f"[scaffold] cascade variables settings unreadable: {exc}",
                    file=sys.stderr,
                )
                if hasattr(self, "_main_window") and self._main_window is not None:
                    self._main_window.statusBar().showMessage(
                        "Cascade variables could not be loaded \u2014 resetting",
                        5000,
                    )
                self._cascade_variables = []

        # Restore cascade name
        name = settings.value("cascade/current_name", "") or ""
        self._current_cascade_name = name if name else None
        self._update_loaded_label()

    # ------------------------------------------------------------------
    # Cascade file save/load/import/export
    # ------------------------------------------------------------------

    def _export_cascade_data(self, name: str, description: str = "") -> dict:
        """Serialize current slots to the cascade file format.

        Paths are relativized against the user-data root first, then the
        bundled root. This produces portable cascade files: user tools end
        up as `tools/foo.json` (resolvable in any install mode), bundled
        tools as `tools/nmap.json` (resolvable via the bundled-fallback in
        _resolve_app_relative). Falls back to the original absolute path
        if neither root contains the file.
        """
        def _relativize(p: str) -> str:
            abs_p = Path(p)
            for base in (_user_data_root(), _bundled_root()):
                try:
                    return str(abs_p.relative_to(base))
                except ValueError:
                    continue
            return p

        steps = []
        for slot in self._slots:
            if not slot.get("tool_path"):
                continue
            tool_rel = _relativize(slot["tool_path"])
            preset_rel = _relativize(slot["preset_path"]) if slot.get("preset_path") else None
            steps.append({
                "tool": tool_rel,
                "preset": preset_rel,
                "delay": slot.get("delay", 0),
                "captures": slot.get("captures", []),
            })
        return {
            "_format": "scaffold_cascade",
            "name": name,
            "description": description,
            "loop_mode": self._loop_enabled,
            "stop_on_error": self._stop_on_error,
            "steps": steps,
            "variables": list(self._cascade_variables),
        }

    def _import_cascade_data(self, data: dict) -> None:
        """Load cascade data from a dict, replacing current slots."""
        fmt = data.get("_format")
        if fmt != "scaffold_cascade":
            raise ValueError(
                f'Invalid or missing _format (expected "scaffold_cascade", got {fmt!r})'
            )
        steps = data.get("steps")
        if not steps or not isinstance(steps, list):
            raise ValueError("Cascade data must contain a non-empty 'steps' list")

        if len(steps) > CASCADE_MAX_SLOTS:
            original_len = len(steps)
            steps = steps[:CASCADE_MAX_SLOTS]
            self._main_window.statusBar().showMessage(
                f"Cascade truncated to {CASCADE_MAX_SLOTS} steps (had {original_len})"
            )

        # Migrate renamed capture sources before validation
        _SOURCE_MIGRATION = {"stdout_full": "stdout_tail", "stderr_full": "stderr_tail"}
        for step in steps:
            for cap in step.get("captures", []):
                if isinstance(cap, dict) and cap.get("source") in _SOURCE_MIGRATION:
                    cap["source"] = _SOURCE_MIGRATION[cap["source"]]

        # Validate captures in all steps before making any changes
        cascade_vars = data.get("variables", [])
        for step_idx, step in enumerate(steps):
            for cap in step.get("captures", []):
                _validate_capture_entry(cap, cascade_vars)

        user_base = _user_data_root()

        # Tear down existing slot widgets (hide + detach synchronously to
        # prevent ghost widgets on Linux where deleteLater timing differs)
        for info in self._slot_widgets:
            info["widget"].hide()
            info["widget"].setParent(None)
            info["widget"].deleteLater()
        self._slot_widgets.clear()
        self._slot_buttons.clear()
        self._arrow_buttons.clear()

        def _resolve_step_path(rel: str) -> str:
            """Resolve a cascade step's relative path. Falls back to user-rooted
            absolute path so missing files still surface in the slot UI."""
            resolved = _resolve_app_relative(rel)
            if resolved is not None:
                return str(resolved)
            return str(user_base / rel)

        # Build new slots from steps
        new_slots = []
        for step_idx, step in enumerate(steps, start=1):
            tool_value = step.get("tool")
            if tool_value and not _cascade_path_is_safe(user_base, tool_value):
                raise ValueError(
                    f"Cascade step {step_idx} has an unsafe tool path: {tool_value!r} "
                    f"(paths cannot escape the scaffold directory via '..')"
                )
            preset_value = step.get("preset")
            if preset_value and not _cascade_path_is_safe(user_base, preset_value):
                raise ValueError(
                    f"Cascade step {step_idx} has an unsafe preset path: {preset_value!r} "
                    f"(paths cannot escape the scaffold directory via '..')"
                )
            tool_path = _resolve_step_path(tool_value) if tool_value else None
            preset_path = _resolve_step_path(preset_value) if preset_value else None
            new_slots.append({
                "tool_path": tool_path,
                "preset_path": preset_path,
                "delay": step.get("delay", 0),
                "captures": step.get("captures", []),
            })

        # Pad to minimum
        while len(new_slots) < CASCADE_INITIAL_SLOTS:
            new_slots.append(self._new_slot())

        self._slots = new_slots
        for i in range(len(self._slots)):
            self._add_slot_widget(i)

        self._loop_enabled = bool(data.get("loop_mode", False))
        self._style_loop_btn()

        self._stop_on_error = bool(data.get("stop_on_error", False))
        self._style_stop_on_error_btn()

        # Load variables, validating each entry has required keys
        variables = []
        for var in data.get("variables", []):
            if not isinstance(var, dict):
                continue
            if "name" not in var or "flag" not in var:
                raise ValueError(
                    "Each cascade variable must have 'name' and 'flag' keys"
                )
            # Sanitize apply_to
            at = var.get("apply_to", "all")
            if at in ("all", "none"):
                var["apply_to"] = at
            elif isinstance(at, list):
                var["apply_to"] = [x for x in at if isinstance(x, int)]
                if not var["apply_to"]:
                    var["apply_to"] = "all"
            else:
                var["apply_to"] = "all"
            variables.append(var)
        self._cascade_variables = variables

        self._current_cascade_name = data.get("name") or None
        self._save_cascade()
        self._refresh_button_labels()
        self._update_add_button_state()
        self._update_remove_button_state()
        self._update_loaded_label()

    def _restore_cascade_config(self, config: dict) -> None:
        """Restore cascade sidebar state from a history config snapshot.

        Similar to _import_cascade_data but operates on the config snapshot
        format (absolute paths, no _format marker, flat slot dicts).
        Missing tool/preset files are silently nulled out (shown as empty slots).
        """
        # Tear down existing slot widgets
        for info in self._slot_widgets:
            info["widget"].hide()
            info["widget"].setParent(None)
            info["widget"].deleteLater()
        self._slot_widgets.clear()
        self._slot_buttons.clear()
        self._arrow_buttons.clear()

        # Build new slots from config snapshot
        _SOURCE_MIGRATION = {"stdout_full": "stdout_tail", "stderr_full": "stderr_tail"}
        new_slots: list[dict] = []
        for slot in config.get("slots", []):
            tp = slot.get("tool_path")
            if tp and not Path(tp).exists():
                tp = None
            pp = slot.get("preset_path")
            if pp and not Path(pp).exists():
                pp = None
            captures = slot.get("captures", [])
            for cap in captures:
                if isinstance(cap, dict) and cap.get("source") in _SOURCE_MIGRATION:
                    cap["source"] = _SOURCE_MIGRATION[cap["source"]]
            new_slots.append({
                "tool_path": tp,
                "preset_path": pp,
                "delay": slot.get("delay", 0),
                "captures": captures,
            })

        # Pad to minimum slot count
        while len(new_slots) < CASCADE_INITIAL_SLOTS:
            new_slots.append(self._new_slot())

        self._slots = new_slots
        for i in range(len(self._slots)):
            self._add_slot_widget(i)

        # Restore loop mode
        self._loop_enabled = bool(config.get("loop_mode", False))
        self._style_loop_btn()

        # Restore stop-on-error
        self._stop_on_error = bool(config.get("stop_on_error", False))
        self._style_stop_on_error_btn()

        # Restore variables
        self._cascade_variables = [dict(v) for v in config.get("variables", [])]

        # Restore cascade name
        name = config.get("cascade_name", "")
        self._current_cascade_name = name if name else None

        self._save_cascade()
        self._refresh_button_labels()
        self._update_add_button_state()
        self._update_remove_button_state()
        self._update_loaded_label()

    def _on_save_cascade_file(self) -> None:
        """Save the current cascade slots to a named JSON file."""
        has_any = any(s.get("tool_path") for s in self._slots)
        if not has_any:
            self._main_window.statusBar().showMessage("No cascade steps to save")
            return

        name, ok = QInputDialog.getText(self, "Save Cascade", "Cascade name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        safe_name = _sanitize_filename_component(name)
        if not safe_name:
            QMessageBox.warning(self, "Invalid Name", "The name is not valid after sanitizing.")
            return

        cascade_path = _cascades_dir() / f"{safe_name}.json"

        # Pre-fill description if overwriting
        existing_desc = ""
        if cascade_path.exists():
            answer = QMessageBox.question(
                self, "Overwrite Cascade",
                f"Overwrite existing cascade '{safe_name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                existing_data = _read_json_file(cascade_path)
                existing_desc = existing_data.get("description", "")
            except (json.JSONDecodeError, OSError):
                pass

        description, ok2 = QInputDialog.getText(
            self, "Save Cascade", "Description (optional):", text=existing_desc,
        )
        if not ok2:
            return
        description = description.strip()

        data = self._export_cascade_data(name, description)
        try:
            _atomic_write_json(cascade_path, data)
        except OSError as e:
            self._main_window.statusBar().showMessage(f"Error saving cascade: {e}")
            return
        self._main_window.statusBar().showMessage(f"Cascade saved: {name}")
        self._current_cascade_name = name
        self._update_loaded_label()

    def _on_load_cascade_list(self) -> None:
        """Show the cascade list dialog and load the selected cascade."""
        has_any = any(s.get("tool_path") for s in self._slots)
        if has_any:
            answer = QMessageBox.question(
                self, "Load Cascade",
                "Loading a cascade will replace current steps. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        dlg = CascadeListDialog(mode="load", parent=self)
        if not dlg.exec() or not dlg.selected_path:
            return

        cascade_path = Path(dlg.selected_path)
        try:
            data = _read_user_json(cascade_path)
        except RuntimeError as e:
            self._main_window.statusBar().showMessage(f"Error loading cascade: {e}")
            return
        except (json.JSONDecodeError, OSError) as e:
            self._main_window.statusBar().showMessage(f"Error loading cascade: {e}")
            return

        if not isinstance(data, dict) or data.get("_format") != "scaffold_cascade":
            QMessageBox.critical(
                self, "Load Failed",
                "This file is not a valid Scaffold cascade file.\n"
                'Expected "_format": "scaffold_cascade".',
            )
            return

        missing_tools, missing_presets = _check_cascade_dependencies(data)
        if missing_tools or missing_presets:
            if not _prompt_cascade_missing_deps(self, missing_tools, missing_presets):
                return

        try:
            self._import_cascade_data(data)
        except ValueError as e:
            self._main_window.statusBar().showMessage(f"Invalid cascade: {e}")
            return

        name = data.get("name", cascade_path.stem)
        missing = 0
        for step in data.get("steps", []):
            if step.get("tool") and _resolve_app_relative(step["tool"]) is None:
                missing += 1
        if missing:
            self._main_window.statusBar().showMessage(
                f"Loaded cascade: {name} \u2014 {missing} tool(s) not found"
            )
        else:
            self._main_window.statusBar().showMessage(f"Loaded cascade: {name}")

    def _on_export_cascade(self) -> None:
        """Export a saved cascade file to a user-chosen location."""
        cascade_dir = _cascades_dir()
        cascades = sorted(cascade_dir.glob("*.json"))
        if not cascades:
            self._main_window.statusBar().showMessage("No cascade files to export")
            return

        dlg = CascadeListDialog(mode="export", parent=self)
        if not dlg.exec() or not dlg.selected_path:
            return

        src = Path(dlg.selected_path)
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Cascade", src.name, "JSON files (*.json)",
        )
        if not dest:
            return

        try:
            content = src.read_text(encoding="utf-8")
            Path(dest).write_text(content, encoding="utf-8")
        except OSError as e:
            self._main_window.statusBar().showMessage(f"Export failed: {e}")
            return
        self._main_window.statusBar().showMessage(f"Cascade exported: {src.stem}")

    def _on_edit_variables(self) -> None:
        """Open the variable definition editor dialog."""
        dlg = CascadeVariableDefinitionDialog(self._cascade_variables, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._cascade_variables = dlg.get_variables()
        self._save_cascade()
        count = len(self._cascade_variables)
        self._main_window.statusBar().showMessage(
            f"Cascade variables updated ({count} defined)"
        )

    def _on_edit_captures(self, slot_index: int) -> None:
        """Open the capture definition editor dialog for a slot."""
        if slot_index < 0 or slot_index >= len(self._slots):
            return
        current = self._slots[slot_index].get("captures", [])
        dlg = CascadeCaptureDefinitionDialog(
            captures=current,
            cascade_variables=self._cascade_variables,
            slot_number=slot_index + 1,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._slots[slot_index]["captures"] = dlg.get_captures()
        self._save_cascade()

    def _on_import_cascade(self) -> None:
        """Import a cascade JSON file into the cascades directory."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Cascade", "", "JSON files (*.json)",
        )
        if not path:
            return
        src = Path(path)

        try:
            data = _read_user_json(src)
        except RuntimeError as e:
            QMessageBox.critical(self, "Import Failed", f"{e}")
            return
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.critical(self, "Import Failed", f"Invalid JSON file:\n{e}")
            return
        if not isinstance(data, dict) or data.get("_format") != "scaffold_cascade":
            QMessageBox.critical(
                self, "Import Failed",
                "This file is not a valid Scaffold cascade file.\n"
                'Expected "_format": "scaffold_cascade".',
            )
            return

        missing_tools, missing_presets = _check_cascade_dependencies(data)
        if missing_tools or missing_presets:
            if not _prompt_cascade_missing_deps(self, missing_tools, missing_presets):
                return

        name = data.get("name", src.stem)
        safe_name = _sanitize_filename_component(name)
        if not safe_name:
            safe_name = src.stem

        dest = _cascades_dir() / f"{safe_name}.json"
        if dest.exists():
            answer = QMessageBox.question(
                self, "Overwrite Cascade",
                f"Overwrite existing cascade '{safe_name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            _atomic_write_json(dest, data)
        except OSError as e:
            self._main_window.statusBar().showMessage(f"Import failed: {e}")
            return
        self._main_window.statusBar().showMessage(f"Cascade imported: {name}")

    # ------------------------------------------------------------------
    # Button labels and styling
    # ------------------------------------------------------------------

    def _refresh_button_labels(self) -> None:
        """Update button text and tooltips to reflect current slot assignments."""
        for i, slot in enumerate(self._slots):
            info = self._slot_widgets[i]
            btn = info["main_btn"]
            arrow = info["arrow_btn"]
            remove = info["remove_btn"]
            if slot["tool_path"]:
                tool_name = Path(slot["tool_path"]).stem
                preset_path = slot.get("preset_path")
                if preset_path:
                    preset_name = Path(preset_path).stem
                    label = preset_name
                else:
                    preset_name = None
                    label = tool_name
                fm = btn.fontMetrics()
                # Available width for text: 220 (content) - 12 (margins) - 16 (num) - 24 (arrow)
                # - 24 (remove) - 6 (spacing) - ~24 (dark mode button padding) ≈ 114px; use 120.
                max_text_width = 220 - 100
                elided = fm.elidedText(label, Qt.TextElideMode.ElideRight, max_text_width)
                btn.setText(elided)
                # Build tooltip
                delay = slot.get("delay", 0)
                tip_lines = [f"Tool: {tool_name}"]
                tip_lines.append(f"Preset: {preset_name if preset_name else '(none)'}")
                if preset_path:
                    # Cache _description in slot dict; invalidate when preset_path changes
                    cached_path = slot.get("_cached_desc_path")
                    if cached_path != preset_path:
                        slot.pop("_cached_description", None)
                        slot["_cached_desc_path"] = preset_path
                    if "_cached_description" not in slot:
                        desc = None
                        try:
                            pdata = _read_json_file(preset_path)
                            desc = pdata.get("_description", None)
                        # broadly defensive — filesystem probe during label refresh; any OS/attribute/lookup error falls back to a default label rather than breaking the UI.
                        except Exception:
                            pass
                        slot["_cached_description"] = desc
                    desc = slot["_cached_description"]
                    if desc:
                        tip_lines.append(f"Description: {desc}")
                    tip_lines.append(f"Path: {preset_path}")
                tip_lines.append(f"Delay: {delay}s")
                btn.setToolTip("\n".join(tip_lines))
                self._style_slot_button(btn, empty=False, arrow_btn=arrow, remove_btn=remove)
            else:
                btn.setText("(empty)")
                btn.setToolTip("Empty slot \u2014 right-click to assign")
                self._style_slot_button(btn, empty=True, arrow_btn=arrow, remove_btn=remove)

    def _style_slot_button(self, btn: QPushButton, empty: bool,
                           arrow_btn: QPushButton | None = None,
                           remove_btn: QPushButton | None = None) -> None:
        """Apply theme-appropriate styling to a slot button, arrow, and remove."""
        if empty:
            dim_color = DARK_COLORS["text_dim"] if _dark_mode else "gray"
            btn.setStyleSheet(f"color: {dim_color}; text-align: center;")
            if arrow_btn:
                arrow_btn.setStyleSheet(f"color: {dim_color}; padding: 0px;" if _dark_mode else f"color: {dim_color};")
            if remove_btn:
                remove_btn.setStyleSheet(f"color: {dim_color}; padding: 0px;" if _dark_mode else f"color: {dim_color};")
        else:
            if _dark_mode:
                text_color = DARK_COLORS["text"]
                btn.setStyleSheet(f"color: {text_color}; text-align: center;")
                if arrow_btn:
                    arrow_btn.setStyleSheet(f"color: {text_color}; padding: 0px;")
                if remove_btn:
                    remove_btn.setStyleSheet(f"color: {text_color}; padding: 0px;")
            else:
                btn.setStyleSheet("text-align: center;")
                if arrow_btn:
                    arrow_btn.setStyleSheet("")
                if remove_btn:
                    remove_btn.setStyleSheet("")

    # ------------------------------------------------------------------
    # Slot interaction
    # ------------------------------------------------------------------

    def _on_slot_clicked(self, slot_index: int) -> None:
        """Left-click: load the assigned tool+preset, or open picker if empty."""
        slot = self._slots[slot_index]
        if not slot["tool_path"]:
            self._on_slot_right_click(slot_index)
            return

        tool_path = slot["tool_path"]
        preset_path = slot.get("preset_path")

        if not Path(tool_path).exists():
            self._main_window.statusBar().showMessage(f"Tool not found: {Path(tool_path).name}")
            return

        self._main_window._load_tool_path(tool_path)

        if preset_path and Path(preset_path).exists():
            try:
                preset_data = _read_user_json(preset_path)
                # Format-marker gate — surface malformed presets before
                # apply_values silently tolerates missing meta keys. Absent
                # _tool is legacy-allowed (mirrors _on_import_preset).
                fmt = preset_data.get("_format")
                current_tool = self._main_window.data["tool"]
                saved_tool = preset_data.get("_tool")
                marker_problem = None
                if fmt is None:
                    marker_problem = 'missing "_format" marker'
                elif fmt == "scaffold_cascade":
                    marker_problem = "is a cascade file, not a preset"
                elif fmt != "scaffold_preset":
                    marker_problem = (
                        f'has "_format": {fmt!r} (expected "scaffold_preset")'
                    )
                elif saved_tool is not None and saved_tool != current_tool:
                    marker_problem = (
                        f'has "_tool": {saved_tool!r} '
                        f'(expected {current_tool!r})'
                    )
                if marker_problem:
                    print(
                        f"[cascade] slot load rejected: {preset_path} \u2014 "
                        f"{marker_problem} (current tool: {current_tool!r})",
                        file=sys.stderr,
                    )
                    self._main_window.statusBar().showMessage(
                        f"Preset not loaded: {Path(preset_path).name} "
                        f"{marker_problem}"
                    )
                    return
                # Structural + per-key type validation \u2014 cascade runs unattended,
                # so malformed preset data (bad types, oversize strings,
                # schema-as-preset) must reject instead of feeding apply_values.
                # report_unknown_keys=False because the schema_hash check below
                # is the canonical stale-preset gate; unknown-key errors here
                # would be redundant noise and would block legacy presets that
                # used to load with a schema-hash warning.
                verrors = validate_preset(
                    preset_data,
                    tool_data=self._main_window.data,
                    report_unknown_keys=False,
                )
                if verrors:
                    preset_name = Path(preset_path).name
                    first_err = verrors[0] if verrors else "unknown"
                    print(
                        f"[cascade] slot load rejected: {preset_path} \u2014 "
                        f"validation failed: {first_err}",
                        file=sys.stderr,
                    )
                    self._main_window.statusBar().showMessage(
                        f"Preset not loaded: {preset_name} \u2014 {first_err}"
                    )
                    return
                had_pw = self._main_window.form.apply_values(preset_data)
                # Schema-hash warning (interactive, non-blocking). Mirrors
                # _on_load_preset UI semantics — slot click is human-initiated,
                # so warn via status bar rather than halt like _chain_advance
                # (unattended). Absent _schema_hash is legacy-allowed.
                preset_name = Path(preset_path).stem
                saved_hash = preset_data.get("_schema_hash")
                hash_mismatch = False
                if saved_hash is not None:
                    current_hash = schema_hash(self._main_window.data)
                    if saved_hash != current_hash:
                        hash_mismatch = True
                        print(
                            f"[cascade] schema_hash mismatch: "
                            f"preset={saved_hash} current={current_hash}",
                            file=sys.stderr,
                        )
                if hash_mismatch:
                    self._main_window.statusBar().showMessage(
                        f"Cascade step loaded: {preset_name} \u2014 Note: this "
                        f"preset was saved with a different schema version. "
                        f"Some fields may not have loaded."
                    )
                elif had_pw:
                    self._main_window.statusBar().showMessage(
                        f"Loaded cascade step: {preset_name} \u2014 "
                        f"password fields must be re-entered"
                    )
                else:
                    self._main_window.statusBar().showMessage(
                        f"Loaded cascade step: {preset_name}"
                    )
            except RuntimeError as e:
                self._main_window.statusBar().showMessage(f"Preset not loaded: {e}")
            except (json.JSONDecodeError, OSError):
                self._main_window.statusBar().showMessage("Preset file could not be loaded")
        elif preset_path:
            self._main_window.statusBar().showMessage(
                f"Preset not found: {Path(preset_path).name} \u2014 tool loaded without preset"
            )

    def _on_slot_right_click(self, slot_index: int) -> None:
        """Right-click: open picker dialog to assign/change/clear the slot."""
        dlg = CascadePickerDialog(slot_index, self._main_window, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._slots[slot_index]["tool_path"] = dlg.selected_tool
            self._slots[slot_index]["preset_path"] = dlg.selected_preset
            self._save_cascade()
            self._refresh_button_labels()

    def _on_clear_all_slots(self) -> None:
        """Clear all cascade slot assignments after confirmation."""
        has_any = any(s.get("tool_path") for s in self._slots)
        if not has_any:
            return
        answer = QMessageBox.question(
            self, "Clear Cascade",
            "Clear all cascade slots?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Remove all slot widgets (hide + detach synchronously to prevent
        # ghost widgets on Linux where deleteLater timing differs)
        for info in self._slot_widgets:
            info["widget"].hide()
            info["widget"].setParent(None)
            info["widget"].deleteLater()
        self._slot_widgets.clear()
        self._slot_buttons.clear()
        self._arrow_buttons.clear()
        # Reset to initial empty slots
        self._slots = [{"tool_path": None, "preset_path": None, "delay": 0}
                       for _ in range(CASCADE_INITIAL_SLOTS)]
        for i in range(CASCADE_INITIAL_SLOTS):
            self._add_slot_widget(i)
        self._refresh_button_labels()
        self._update_add_button_state()
        self._update_remove_button_state()
        self._current_cascade_name = None
        self._save_cascade()
        self._update_loaded_label()
        self._main_window.statusBar().showMessage("All cascade slots cleared")

    # ------------------------------------------------------------------
    # Chain runner
    # ------------------------------------------------------------------

    def _build_cascade_config_snapshot(self) -> dict:
        """Build a config snapshot for cascade history recording."""
        return {
            "slots": [
                {
                    "tool_path": s.get("tool_path"),
                    "preset_path": s.get("preset_path"),
                    "delay": s.get("delay", 0),
                    "captures": s.get("captures", []),
                }
                for s in self._slots
            ],
            "variables": [dict(v) for v in self._cascade_variables],
            "loop_mode": self._loop_enabled,
            "stop_on_error": self._stop_on_error,
            "cascade_name": self._current_cascade_name or "",
        }

    def _on_run_chain(self) -> None:
        """Start running all assigned cascade steps sequentially."""
        queue = []
        for i, slot in enumerate(self._slots):
            if slot.get("tool_path") and Path(slot["tool_path"]).exists():
                queue.append(i)

        if not queue:
            self._main_window.statusBar().showMessage("No valid cascade steps to run")
            return

        # Prompt for cascade variable values if any are defined
        if self._cascade_variables:
            dlg = CascadeVariableDialog(self._cascade_variables, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self._chain_variable_values = dlg.get_values()

        self._chain_queue = queue
        self._chain_current = -1
        self._chain_loop_count = 0
        self._chain_state = CHAIN_LOADING

        # Record cascade run start
        self._cascade_run_loop_index = 0
        self._cascade_steps = []
        self._cascade_step_start = None
        self._cascade_finish_status = None
        self._cascade_run_id = uuid.uuid4().hex
        run_entry = {
            "id": self._cascade_run_id,
            "name": self._current_cascade_name or "",
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "loop_index": 0,
            "stop_on_error": self._stop_on_error,
            "steps": [],
            "config": self._build_cascade_config_snapshot(),
        }
        self._main_window._append_cascade_run(run_entry)

        # Disable UI during chain
        self.run_chain_btn.setEnabled(False)
        self.run_chain_btn.setText("Running...")
        self.stop_chain_btn.setEnabled(True)
        self.pause_chain_btn.setEnabled(True)
        self.pause_chain_btn.setText("Pause")
        self.clear_all_btn.setEnabled(False)
        self.loop_btn.setEnabled(False)
        self.stop_on_error_btn.setEnabled(False)
        self.add_step_btn.setEnabled(False)
        for info in self._slot_widgets:
            info["main_btn"].setEnabled(False)
            info["arrow_btn"].setEnabled(False)
            info["remove_btn"].setEnabled(False)
            info["delay_spin"].setEnabled(False)

        # Disable File menu actions to prevent manual interference
        self._main_window.act_load.setEnabled(False)

        # Start first step
        self._chain_advance()

    def _substitute_captures(self, text: str, values: dict) -> str:
        """Replace {name} tokens in text using values dict.

        Emits a warning line to the output panel for each unset capture name.
        Emits argv-flag-injection warnings for whole-value flag-like captures.
        If _stop_on_error is True and any captures are unset or flag-like,
        halts the chain.  Returns the substituted text string.
        """
        # Argv-flag injection guard: check for whole-value flag-like captures
        # before substitution.  A field value of exactly "{name}" where
        # values[name] matches ^--?[A-Za-z] is suspicious.
        for name, value in values.items():
            if (
                text == f"{{{name}}}"
                and isinstance(value, str)
                and _looks_like_argv_flag(value)
                and name not in self._argv_warned_captures
            ):
                self._argv_warned_captures.add(name)
                if hasattr(self._main_window, "output"):
                    self._main_window._append_output(
                        f"[cascade] capture '{name}' resolved to argv-flag-like value "
                        f"'{value}'; review before next step runs\n",
                        COLOR_WARN,
                    )
                if self._stop_on_error:
                    self._cascade_finish_status = "error_halted"
                    self._chain_cleanup(
                        f"Cascade stopped: capture '{name}' resolved to "
                        f"argv-flag-like value '{value}'"
                    )
                    return text  # halt — return original, cleanup already ran

        result, unset_names = substitute_captures(text, values)
        for name in unset_names:
            if name in self._unset_warned_captures:
                continue
            self._unset_warned_captures.add(name)
            if hasattr(self._main_window, 'output'):
                self._main_window._append_output(
                    f"[cascade] capture '{name}' did not match; substituting empty string\n",
                    COLOR_WARN,
                )
        if unset_names and self._stop_on_error:
            first = unset_names[0]
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup(
                f"Cascade stopped: capture '{first}' was not set"
            )
            return text  # halt — return original, cleanup already ran
        return result

    def _substitute_in_form_fields(self, form) -> None:
        """Replace {capture_name} tokens in all form field values.

        Reads and writes via the form's own methods so every widget type
        (string, text, file, directory, password, enum) is handled.
        Lists (multi_enum) have each string element substituted.

        Uses collect-then-commit: pending writes are gathered first and
        only applied after the full loop completes without a halt.
        """
        if not self._chain_variable_values:
            return
        pending: list[tuple[tuple, object]] = []
        for key in list(form.fields.keys()):
            current = form._raw_field_value(key)
            if isinstance(current, str):
                if "{" not in current:
                    continue
                new_value = self._substitute_captures(current, self._chain_variable_values)
                if self._chain_state == CHAIN_IDLE:
                    return  # guard or unset halt fired — discard all pending writes
                if new_value != current:
                    pending.append((key, new_value))
            elif isinstance(current, list):
                changed = False
                new_list = []
                for item in current:
                    if isinstance(item, str) and "{" in item:
                        new_item = self._substitute_captures(item, self._chain_variable_values)
                        if self._chain_state == CHAIN_IDLE:
                            return  # discard all pending writes
                        if new_item != item:
                            changed = True
                        new_list.append(new_item)
                    else:
                        new_list.append(item)
                if changed:
                    pending.append((key, new_list))
        # Commit all writes atomically — only reached if no halt fired
        for key, new_value in pending:
            form._set_field_value(key, new_value)

    def _substitute_in_extra_flags(self, form) -> None:
        """Substitute {capture} tokens in the Additional Flags buffer.

        Uses single-token semantics: each {name} must resolve to
        exactly one shlex token. If any capture resolves to
        multi-token text (e.g. contains a space outside quotes, or
        a shell metacharacter that shlex treats as a separator),
        halt the chain with a clear error naming the capture and
        its resolved value.

        Leaves the buffer unchanged if there are no {name} tokens
        that match defined captures. Unset captures fall through
        to substitute_captures' existing empty-string behavior.
        """
        if not self._chain_variable_values:
            return
        if not hasattr(form, "extra_flags_edit") or form.extra_flags_edit is None:
            return
        original = form.extra_flags_edit.toPlainText()

        # Pre-validate: every {name} in the buffer whose name
        # resolves to a multi-token value must halt. We do this
        # BEFORE calling _substitute_captures so the halt message
        # can name the specific capture and its resolved value.
        import shlex as _shlex
        pattern = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
        for m in pattern.finditer(original):
            name = m.group(1)
            if name not in self._chain_variable_values:
                continue  # unset — let _substitute_captures handle (becomes empty)
            value = str(self._chain_variable_values[name])
            try:
                tokens = _shlex.split(value)
            except ValueError as exc:
                self._cascade_finish_status = "error_halted"
                self._chain_cleanup(
                    f"Cascade stopped: capture '{name}' has unparseable "
                    f"value {value!r} for extra flags ({exc})"
                )
                return
            if len(tokens) != 1:
                self._cascade_finish_status = "error_halted"
                self._chain_cleanup(
                    f"Cascade stopped: capture '{name}' resolved to "
                    f"multi-token value {value!r} in extra flags "
                    f"(expected single token, got {len(tokens)})"
                )
                return

        # All {captures} in the buffer resolve to single tokens
        # (or are unset). Delegate to _substitute_captures for the
        # actual rewrite — it handles the unset-capture warnings
        # and the argv-flag injection guard.
        new_text = self._substitute_captures(
            original, self._chain_variable_values
        )
        if self._chain_state == CHAIN_IDLE:
            return  # halt fired inside _substitute_captures
        if new_text != original:
            form.extra_flags_edit.setPlainText(new_text)

    def _chain_advance(self) -> None:
        """Load and run the next cascade step in the chain."""
        if self._chain_state == CHAIN_IDLE:
            return
        self._chain_current += 1

        if self._chain_current >= len(self._chain_queue):
            if self._loop_enabled:
                # Finish current cascade run record
                if self._cascade_run_id is not None:
                    self._main_window._update_cascade_run(self._cascade_run_id, {
                        "status": "completed",
                        "finished_at": time.time(),
                        "steps": list(self._cascade_steps),
                    })
                self._chain_loop_count += 1
                self._chain_current = -1
                # Start new cascade run for next loop iteration
                self._cascade_run_loop_index += 1
                self._cascade_steps = []
                self._cascade_step_start = None
                self._cascade_run_id = uuid.uuid4().hex
                run_entry = {
                    "id": self._cascade_run_id,
                    "name": self._current_cascade_name or "",
                    "status": "running",
                    "started_at": time.time(),
                    "finished_at": None,
                    "loop_index": self._cascade_run_loop_index,
                    "stop_on_error": self._stop_on_error,
                    "steps": [],
                    "config": self._build_cascade_config_snapshot(),
                }
                self._main_window._append_cascade_run(run_entry)
                self._main_window.statusBar().showMessage(
                    f"Cascade: starting loop {self._chain_loop_count + 1}"
                )
                last_slot_index = self._chain_queue[-1]
                delay_secs = self._slots[last_slot_index].get("delay", 0)
                delay_ms = max(delay_secs * 1000, 300)
                QTimer.singleShot(delay_ms, self._chain_advance)
                return
            else:
                runs = self._chain_loop_count + 1
                self._cascade_finish_status = "completed"
                self._chain_cleanup(f"Cascade complete ({runs} run(s))")
                return

        slot_index = self._chain_queue[self._chain_current]
        self._argv_warned_captures.clear()
        self._unset_warned_captures.clear()
        slot = self._slots[slot_index]
        tool_path = slot["tool_path"]
        preset_path = slot.get("preset_path")

        # Highlight the active slot button
        self._highlight_active_slot(slot_index)

        # Status update
        step = self._chain_current + 1
        total = len(self._chain_queue)
        loop_prefix = f"loop {self._chain_loop_count + 1}, " if self._chain_loop_count > 0 else ""
        self._main_window.statusBar().showMessage(
            f"Cascade: {loop_prefix}step {step}/{total} \u2014 {Path(tool_path).stem}"
        )

        # Preserve output from previous chain steps
        self._main_window._chain_preserve_output = True
        if self._chain_current == 0 and self._chain_loop_count > 0 and hasattr(self._main_window, 'output'):
            sep = f"\n{'\u2550' * 60}\n  Loop {self._chain_loop_count + 1}\n{'\u2550' * 60}\n"
            self._main_window._append_output(sep, "gray")
        elif self._chain_current > 0 and hasattr(self._main_window, 'output'):
            sep = f"\n{'\u2500' * 60}\n"
            self._main_window._append_output(sep, "gray")

        # Clear stale recovery file before loading next tool
        if hasattr(self._main_window, '_clear_recovery_file'):
            self._main_window._clear_recovery_file()

        # Snapshot in-progress form values if this slot uses the same tool
        # currently loaded — preserves required fields the user just typed.
        snapshot = None
        try:
            current_path = self._main_window.tool_path
            if (
                current_path
                and self._main_window.form is not None
                and Path(current_path).resolve() == Path(tool_path).resolve()
            ):
                snapshot = self._main_window.form.serialize_values()
        # broadly defensive — cascade advance touches schema/preset/Qt state; narrowing would risk aborting a cascade on an unanticipated error class.
        except Exception:
            snapshot = None

        # Load tool
        try:
            self._main_window._load_tool_path(tool_path)
        except (AttributeError, RuntimeError) as e:
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup(f"Cascade failed loading tool: {e}")
            return
        # Guard: _load_tool_path swallows errors internally (shows a modal
        # dialog and returns None on failure).  If the load didn't actually
        # change tool_path to the expected value, the form still has the
        # previous step's tool — halt instead of running the wrong binary.
        if self._main_window.tool_path != tool_path:
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup(
                f"Cascade failed loading tool: {Path(tool_path).stem}"
            )
            return

        # Disable main window controls (_load_tool_path re-enables them)
        self._main_window.run_btn.setEnabled(False)
        self._main_window.act_back.setEnabled(False)
        self._main_window.act_reload.setEnabled(False)

        # Apply preset
        if preset_path and Path(preset_path).exists():
            try:
                preset_data = _read_user_json(preset_path)
                # Format-marker gate: cascades run unattended, so a preset
                # missing _format, or with wrong _format / _tool, would
                # silently produce a wrong command via apply_values' tolerant
                # handling of missing meta keys. UI mode (_on_import_preset)
                # prompts instead; here we halt. Absent _tool is legacy-allowed
                # (mirrors UI guard). Runs before _schema_hash so marker errors
                # are named specifically instead of masked by a hash mismatch.
                fmt = preset_data.get("_format")
                current_tool = self._main_window.data["tool"]
                saved_tool = preset_data.get("_tool")
                marker_problem = None
                if fmt is None:
                    marker_problem = 'missing "_format" marker'
                elif fmt == "scaffold_cascade":
                    marker_problem = "is a cascade file, not a preset"
                elif fmt != "scaffold_preset":
                    marker_problem = (
                        f'has "_format": {fmt!r} (expected "scaffold_preset")'
                    )
                elif saved_tool is not None and saved_tool != current_tool:
                    marker_problem = (
                        f'has "_tool": {saved_tool!r} '
                        f'(expected {current_tool!r})'
                    )
                if marker_problem:
                    preset_name = Path(preset_path).name
                    print(
                        f"[cascade] preset rejected: {preset_path} \u2014 "
                        f"{marker_problem} (current tool: {current_tool!r})",
                        file=sys.stderr,
                    )
                    self._cascade_finish_status = "error_halted"
                    self._chain_cleanup(
                        f"Cascade stopped: preset {preset_name} "
                        f"{marker_problem} for step "
                        f"{self._chain_current + 1}"
                    )
                    return
                # Structural + per-key type validation \u2014 same rationale as
                # _on_slot_clicked. Runs before the schema-hash check so structural
                # errors name themselves specifically rather than being masked by
                # a hash mismatch. report_unknown_keys=False so the schema_hash
                # check below remains the canonical stale-preset gate.
                verrors = validate_preset(
                    preset_data,
                    tool_data=self._main_window.data,
                    report_unknown_keys=False,
                )
                if verrors:
                    preset_name = Path(preset_path).name
                    first_err = verrors[0] if verrors else "unknown"
                    print(
                        f"[cascade] preset rejected: {preset_path} \u2014 "
                        f"validation failed: {first_err}",
                        file=sys.stderr,
                    )
                    self._cascade_finish_status = "error_halted"
                    self._chain_cleanup(
                        f"Cascade stopped: preset {preset_name} failed validation "
                        f"for step {self._chain_current + 1} \u2014 {first_err}"
                    )
                    return
                # Schema-hash gate: cascades run unattended, so a stale preset
                # would silently produce a wrong command. UI mode (line ~8931)
                # warns instead — correct there, since a human can react.
                # Absent _schema_hash is treated as legacy (mirrors UI guard).
                saved_hash = preset_data.get("_schema_hash")
                if saved_hash is not None:
                    current_hash = schema_hash(self._main_window.data)
                    if saved_hash != current_hash:
                        preset_name = Path(preset_path).stem
                        tool_name = self._main_window.data["tool"]
                        print(
                            f"[cascade] schema_hash mismatch: "
                            f"preset={saved_hash} current={current_hash}",
                            file=sys.stderr,
                        )
                        self._cascade_finish_status = "error_halted"
                        self._chain_cleanup(
                            f"Cascade stopped: preset schema changed for step "
                            f"{self._chain_current + 1} \u2014 {preset_name} "
                            f"(re-save against current {tool_name} schema)"
                        )
                        return
                # Return value (had_passwords) intentionally unused — chain runs
                # are automated and cannot prompt for password re-entry.
                self._main_window.form.apply_values(preset_data)
            except RuntimeError as e:
                preset_name = Path(preset_path).name
                self._cascade_finish_status = "error_halted"
                self._chain_cleanup(
                    f"Cascade stopped: preset {preset_name} — {e}"
                )
                return
            except (json.JSONDecodeError, OSError) as e:
                self._cascade_finish_status = "error_halted"
                self._chain_cleanup(f"Cascade failed loading preset: {e}")
                return
        elif preset_path:
            # Preset file configured but missing — halt rather than
            # run with default values, which would silently change
            # the tool's behavior.
            preset_name = Path(preset_path).stem
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup(
                f"Cascade stopped: preset not found for step "
                f"{self._chain_current + 1} \u2014 {preset_name}"
            )
            return

        # Overlay snapshot AFTER preset so user edits win. The snapshot captures
        # the form state at the start of THIS step (before preset load), so keys
        # present in the snapshot represent values that existed going into the
        # step — we merge those OVER the preset. Fields absent from the snapshot
        # retain their preset value. This fixes a bug where a full apply_values
        # reset clobbered the just-applied preset for fields the snapshot didn't
        # mention (common case: step N-1 and step N share a tool, step N-1's
        # snapshot lacks step N's preset-only fields, apply_values zeroed them).
        # Intentional fallback: if overlay fails for a specific key, continue
        # with the preset-only value for that key rather than halting the chain.
        if snapshot is not None and self._main_window.form is not None:
            form = self._main_window.form
            try:
                for key, field in form.fields.items():
                    scope, flag = key
                    pk = flag if scope == form.GLOBAL else f"{scope}:{flag}"
                    if pk in snapshot:
                        try:
                            form._set_field_value(key, snapshot[pk])
                        # intentional fallback — see comment block at the top of this overlay loop; per-key overlay failure continues with the preset value rather than halting the chain.
                        except Exception:
                            pass
                # _extra_flags is not a field key; handle separately.
                # Absent from snapshot → preset's extra_flags survives.
                if "_extra_flags" in snapshot and hasattr(form, "extra_flags_edit"):
                    form.extra_flags_edit.setPlainText(snapshot["_extra_flags"])
                    if hasattr(form, "extra_flags_group"):
                        form.extra_flags_group.setChecked(True)
            # intentional fallback — extra_flags overlay failure continues with preset's extra_flags rather than halting the chain.
            except Exception:
                pass

        # Inject cascade variable values into the form
        if self._chain_variable_values:
            form = self._main_window.form
            # Variable injection matches by flag name against the form's fields.
            # Only the FIRST matching field in iteration order receives the value
            # (note the ``break`` below).  If a form has the same flag in both
            # global scope and a subcommand scope, only one will be set — whichever
            # key appears first in form.fields.  This is intentional: variables
            # target a single logical field, not every occurrence of the flag.
            for var in self._cascade_variables:
                apply_to = var.get("apply_to", "all")
                if apply_to == "none":
                    continue
                if apply_to == "all" or (isinstance(apply_to, list) and slot_index in apply_to):
                    flag = var.get("flag", "")
                    if flag in self._chain_variable_values:
                        value = self._chain_variable_values[flag]
                        for key in form.fields:
                            scope, flag_name = key
                            pk = flag_name if scope == form.GLOBAL else f"{scope}:{flag_name}"
                            if pk == flag:
                                form._set_field_value(key, value)
                                break

        # Substitution pass: replace {capture_name} tokens in all field values.
        # Runs after variable injection so injected values are also subject to
        # substitution (a variable value can legitimately contain {capture_name}).
        # Delegates to _substitute_in_form_fields which handles all widget types
        # via the form's own reader/writer methods.
        form = self._main_window.form
        if form is not None and self._chain_variable_values:
            self._substitute_in_form_fields(form)
            if self._chain_state == CHAIN_IDLE:
                return  # guard or unset halt fired
        if form is not None and self._chain_variable_values:
            self._substitute_in_extra_flags(form)
            if self._chain_state == CHAIN_IDLE:
                return  # multi-token halt fired

        # Small delay to let the form render before executing
        QTimer.singleShot(150, self._chain_execute_current)

    def _chain_execute_current(self) -> None:
        """Execute the currently loaded tool after form has rendered."""
        if self._chain_state != CHAIN_LOADING:
            return  # Chain was cancelled during the delay

        # Validate required fields
        missing = self._main_window.form.validate_required()
        if missing:
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup("Cascade stopped: required fields missing")
            return

        self._chain_state = CHAIN_RUNNING

        # Restore real handler before wrapping, so we don't stack wrappers
        if self._stored_original_on_finished is not None:
            self._main_window._on_finished = self._stored_original_on_finished

        # Monkey-patch _on_finished to advance the chain after each step
        original_on_finished = self._main_window._on_finished

        try:
            def _chain_on_finished(exit_code, exit_status):
                """Wrapper that calls original handler then advances chain."""
                try:
                    original_on_finished(exit_code, exit_status)
                    if self._chain_state == CHAIN_IDLE:
                        return  # Chain was stopped externally; don't re-disable
                    # Re-disable run_btn (original handler re-enables it)
                    self._main_window.run_btn.setEnabled(False)
                    # Extract captures from this step's output
                    slot_idx = self._chain_queue[self._chain_current]
                    slot_captures = self._slots[slot_idx].get("captures", [])
                    captured = {}
                    capture_errors: list = []
                    if slot_captures:
                        captured, capture_errors = extract_captures(
                            slot_captures,
                            self._main_window._last_run_stdout,
                            self._main_window._last_run_stderr,
                            exit_code,
                        )
                        for _cap_name, _cap_err, _cap_type in capture_errors:
                            if hasattr(self._main_window, "output"):
                                self._main_window._append_output(
                                    f"[cascade] capture '{_cap_name}' failed ({_cap_type}): {_cap_err}\n",
                                    COLOR_WARN,
                                )
                        self._chain_variable_values.update(captured)
                    # Record cascade history step
                    if self._cascade_run_id is not None:
                        _err_tok = None
                        if self._main_window._timed_out:
                            _err_tok = "timed_out"
                        elif exit_status == QProcess.ExitStatus.CrashExit:
                            _err_tok = "crashed"
                        _cslot = self._slots[slot_idx]
                        _step = self._build_chain_step_record(
                            self._chain_current, _cslot, exit_code, _err_tok,
                            captures=dict(captured),
                        )
                        self._cascade_steps.append(_step)
                        self._main_window._update_cascade_run(
                            self._cascade_run_id,
                            {"steps": list(self._cascade_steps)},
                        )
                    if self._chain_state == CHAIN_RUNNING and capture_errors:
                        pool_failed = any(et == "pool_error" for _, _, et in capture_errors)
                        other_errors = [e for e in capture_errors if e[2] != "pool_error"]
                        if pool_failed:
                            self._cascade_finish_status = "error_halted"
                            self._chain_cleanup(
                                f"Cascade stopped: regex pool unavailable at step "
                                f"{self._chain_current + 1}"
                            )
                            return
                        if other_errors and self._stop_on_error:
                            first_name, first_msg, first_type = other_errors[0]
                            self._cascade_finish_status = "error_halted"
                            self._chain_cleanup(
                                f"Cascade stopped: capture '{first_name}' failed "
                                f"({first_type}): {first_msg}"
                            )
                            return
                    if self._chain_state == CHAIN_RUNNING:
                        if self._stop_on_error and exit_code != 0:
                            slot_idx = self._chain_queue[self._chain_current]
                            step_num = self._chain_current + 1
                            tool_name = Path(self._slots[slot_idx]["tool_path"]).stem
                            self._cascade_finish_status = "error_halted"
                            self._chain_cleanup(
                                f"Cascade stopped: step {step_num} ({tool_name}) "
                                f"exited with code {exit_code}"
                            )
                            return
                        if self._chain_paused:
                            self._chain_state = CHAIN_PAUSED
                            self.pause_chain_btn.setEnabled(True)
                            self.pause_chain_btn.setText("Resume")
                            next_step = self._chain_current + 2  # 1-indexed display
                            self._main_window.statusBar().showMessage(
                                f"Cascade paused before step {next_step}. Click Resume to continue."
                            )
                            return
                        self._chain_state = CHAIN_LOADING
                        slot_idx = self._chain_queue[self._chain_current]
                        delay_secs = self._slots[slot_idx].get("delay", 0)
                        delay_ms = max(delay_secs * 1000, 300)  # Minimum 300ms for UI breathing room
                        if delay_secs > 0:
                            next_step = self._chain_current + 2  # 1-indexed display
                            self._main_window.statusBar().showMessage(
                                f"Cascade: waiting {delay_secs}s before step {next_step}..."
                            )
                        QTimer.singleShot(delay_ms, self._chain_advance)
                # broadly defensive — wraps the full Qt on-finished callback body (~90 lines across multiple helpers); narrowing would risk leaving the cascade in a non-idle state on an unanticipated error.
                except Exception as _wrap_exc:
                    # Restore original handler so main window is usable
                    if self._stored_original_on_finished is not None:
                        try:
                            self._main_window._on_finished = self._stored_original_on_finished
                        except (AttributeError, RuntimeError):
                            pass
                    print(
                        f"[scaffold] cascade step handler crashed: {_wrap_exc}",
                        file=sys.stderr,
                    )
                    self._cascade_finish_status = "error_halted"
                    try:
                        self._chain_cleanup(
                            f"Cascade crashed in step handler: {_wrap_exc}"
                        )
                    # broadly defensive — chain-finished handler touches multiple subsystem states; narrowing would risk stranding the chain on an unexpected error.
                    except Exception:
                        # Last-resort: unfreeze run button so UI stays usable
                        try:
                            self._main_window.run_btn.setEnabled(True)
                            self.run_chain_btn.setEnabled(True)
                        except (AttributeError, RuntimeError):
                            pass

            self._main_window._on_finished = _chain_on_finished
            if self._stored_original_on_finished is None:
                self._stored_original_on_finished = original_on_finished

            # Record step start time for cascade history
            self._cascade_step_start = time.time()

            # Start the command
            self._main_window._on_run_stop()

            # If process failed to even start (e.g. binary not found),
            # _on_error fires and sets process=None without _on_finished.
            # Detect this and clean up.
            if self._main_window.process is None and self._chain_state == CHAIN_RUNNING:
                # Record the failed step in cascade history
                if self._cascade_run_id is not None:
                    _fidx = self._chain_queue[self._chain_current]
                    _fsl = self._slots[_fidx]
                    self._cascade_steps.append(self._build_chain_step_record(
                        self._chain_current, _fsl, None, "failed_to_start",
                    ))
                self._cascade_finish_status = "error_halted"
                self._chain_cleanup("Cascade stopped: process failed to start")
        # broadly defensive — wraps the entire cascade-step setup/launch path (closure definition, handler install, process start, failed-start cleanup); narrowing would risk stranding the chain on an unexpected error class.
        except Exception as e:
            try:
                self._main_window._on_finished = original_on_finished
            except (AttributeError, RuntimeError):
                pass
            self._cascade_finish_status = "error_halted"
            self._chain_cleanup(f"Cascade crashed: {e}")

    def _on_pause_chain(self) -> None:
        """Pause or resume the cascade chain."""
        if self._chain_state == CHAIN_RUNNING:
            # Set the flag — actual pause happens when current step finishes
            self._chain_paused = True
            self.pause_chain_btn.setEnabled(False)  # Re-enabled when state becomes PAUSED
            self._main_window.statusBar().showMessage(
                "Cascade will pause after current step completes..."
            )
        elif self._chain_state == CHAIN_PAUSED:
            # Resume
            self._chain_paused = False
            self._chain_state = CHAIN_LOADING
            self.pause_chain_btn.setText("Pause")
            self._main_window.statusBar().showMessage("Cascade resumed")
            QTimer.singleShot(100, self._chain_advance)

    def _build_chain_step_record(
        self,
        step_index: int,
        slot: dict,
        exit_code: int | None,
        error: str | None,
        captures: dict | None = None,
        finished_at: float | None = None,
    ) -> dict:
        """Build a cascade history step record dict.

        Shared by _chain_on_finished, the failed-to-start path, and
        _on_stop_chain so the step record shape stays consistent.
        """
        return {
            "index": step_index,
            "tool_name": Path(slot["tool_path"]).stem,
            "tool_path": slot["tool_path"],
            "preset_path": slot.get("preset_path"),
            "command": getattr(self._main_window, '_history_display', '') or '',
            "exit_code": exit_code,
            "error": error,
            "started_at": self._cascade_step_start,
            "finished_at": finished_at if finished_at is not None else time.time(),
            "captures": captures if captures is not None else {},
        }

    def _on_stop_chain(self) -> None:
        """Cancel the chain and stop the current process."""
        was_running = self._chain_state
        self._chain_state = CHAIN_IDLE
        # Synthesize a step record for the in-progress step before killing
        # the process.  Must happen before _on_run_stop() so that the async
        # _chain_on_finished callback (which early-returns on CHAIN_IDLE)
        # won't duplicate the record.
        try:
            if (
                was_running == CHAIN_RUNNING
                and self._cascade_run_id is not None
                and self._cascade_steps is not None
                and 0 <= self._chain_current < len(self._chain_queue)
            ):
                _sidx = self._chain_queue[self._chain_current]
                _sslot = self._slots[_sidx]
                self._cascade_steps.append(self._build_chain_step_record(
                    self._chain_current, _sslot, None, "stopped",
                ))
        # broadly defensive — stop handler must not itself throw; any error during teardown is logged elsewhere or swallowed to guarantee the chain reaches an idle state.
        except Exception:
            pass  # Never crash the stop path
        if was_running == CHAIN_RUNNING:
            if self._main_window.process and self._main_window.process.state() != QProcess.ProcessState.NotRunning:
                self._main_window._on_run_stop()  # Triggers the stop path
        self._cascade_finish_status = "stopped"
        self._chain_cleanup("Cascade cancelled")

    def _chain_cleanup(self, message: str, silent: bool = False) -> None:
        """Reset chain state and re-enable UI."""
        # Record terminal cascade history transition
        if self._cascade_run_id is not None and self._cascade_finish_status:
            self._main_window._update_cascade_run(self._cascade_run_id, {
                "status": self._cascade_finish_status,
                "finished_at": time.time(),
                "steps": list(self._cascade_steps),
            })
        self._cascade_run_id = None
        self._cascade_steps = []
        self._cascade_step_start = None
        # _cascade_finish_status is intentionally NOT reset here. It persists
        # until the next _on_run_chain() so callers (and Section 179 tests)
        # can read the final status of the last run. The field is reset to
        # None at the start of each new run at _on_run_chain (~line 6900).

        self._chain_state = CHAIN_IDLE
        self._chain_paused = False
        self._chain_queue = []
        self._chain_current = -1
        self._chain_variable_values = {}
        self._main_window._chain_preserve_output = False
        self._main_window._autosave_timer.stop()

        # Restore original _on_finished if it was replaced
        if self._stored_original_on_finished is not None:
            self._main_window._on_finished = self._stored_original_on_finished
            self._stored_original_on_finished = None

        # Re-enable UI
        self.run_chain_btn.setEnabled(True)
        self.run_chain_btn.setText("Run")
        self.pause_chain_btn.setEnabled(False)
        self.pause_chain_btn.setText("Pause")
        self.stop_chain_btn.setEnabled(False)
        self.clear_all_btn.setEnabled(True)
        self.loop_btn.setEnabled(True)
        self.stop_on_error_btn.setEnabled(True)
        self.add_step_btn.setEnabled(len(self._slots) < CASCADE_MAX_SLOTS)
        for info in self._slot_widgets:
            info["main_btn"].setEnabled(True)
            info["arrow_btn"].setEnabled(True)
            info["remove_btn"].setEnabled(True)
            info["delay_spin"].setEnabled(True)
        self._update_remove_button_state()
        self._main_window.run_btn.setEnabled(True)
        self._main_window.act_load.setEnabled(True)
        # Only re-enable these if a tool is loaded
        if self._main_window.data is not None:
            self._main_window.act_back.setEnabled(True)
            self._main_window.act_reload.setEnabled(True)

        self._clear_slot_highlights()
        if not silent:
            self._main_window.statusBar().showMessage(message)

        # Re-evaluate run button state based on required fields
        if hasattr(self._main_window, 'form') and self._main_window.form:
            self._main_window._update_preview()

    def _highlight_active_slot(self, index: int) -> None:
        """Highlight the currently executing slot button."""
        self._clear_slot_highlights()
        color = DARK_COLORS["success"] if _dark_mode else "#4CAF50"
        if _dark_mode:
            text_color = DARK_COLORS["text"]
            self._slot_buttons[index].setStyleSheet(
                f"color: {text_color}; text-align: center; padding: 4px 12px; border: 2px solid {color};")
            self._arrow_buttons[index].setStyleSheet(
                f"color: {text_color}; padding: 0px; border: 2px solid {color};")
        else:
            self._slot_buttons[index].setStyleSheet(f"border: 2px solid {color};")
            self._arrow_buttons[index].setStyleSheet(f"border: 2px solid {color};")

    def _clear_slot_highlights(self) -> None:
        """Remove highlighting from all slot buttons."""
        self._refresh_button_labels()

    def update_theme(self) -> None:
        """Re-apply theme colors to slot buttons and chain controls."""
        # Refresh chain QSS first so _style_loop_btn's OFF branch picks up the
        # current theme border; loop_btn/stop_on_error_btn are handled via
        # _style_*_btn, the remaining plain chain buttons below.
        _border = DARK_COLORS["border"] if _dark_mode else "#c0c0c0"
        self._chain_qss = f"QPushButton {{ border: 1px solid {_border}; {self._chain_btn_body} }}"
        self._refresh_button_labels()
        self._clear_slot_highlights()
        self._style_loop_btn()
        dim_color = DARK_COLORS["text_dim"] if _dark_mode else "#666666"
        for info in self._slot_widgets:
            info["delay_label"].setStyleSheet(f"font-size: 11px; color: {dim_color};")
        for _cbtn in (self.run_chain_btn, self.pause_chain_btn, self.stop_chain_btn,
                      self.clear_all_btn, self.add_step_btn):
            _cbtn.setStyleSheet(self._chain_qss)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Main application window managing the tool picker, form view, and process execution."""

    # Class-level test escape hatch: when True, skip scheduling the first-run
    # welcome dialog timer entirely. Tests set this to avoid blocking modals
    # without depending on QSettings persistence (which can flip between
    # registry and INI backends mid-test in portable-mode sections).
    _suppress_welcome_dialog: bool = False

    def __init__(self, tool_path=None):
        super().__init__()
        self.data = None
        self.tool_path = None
        self.process = None
        self._killed = False
        self._error_reported = False
        self._timed_out = False
        self._elevated_run = False
        self._run_start_time: float | None = None
        self._force_kill_timer = QTimer(self)
        self._force_kill_timer.setSingleShot(True)
        self._force_kill_timer.setInterval(2000)
        self._force_kill_timer.timeout.connect(self._on_force_kill)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self.settings = _create_settings()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._autosave_form)
        # Per-form-view timers lifted to __init__ so they aren't recreated and
        # leaked on every form reload (R2-A2). Lambdas/bound methods resolve
        # self.status / self._flush_output / self._on_timeout_fired fresh at
        # fire time, so rebuilding the form view is safe.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status.setText("") if hasattr(self, "status") else None)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout_fired)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(OUTPUT_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_output)
        self._autosave_warned = False
        self._default_form_snapshot = None
        self._chain_preserve_output = False
        self._copy_password_choice: bool | None = None
        # Cache bound method to preserve identity across attribute accesses.
        # Python creates a fresh bound-method wrapper on each `self._on_finished`
        # read, which breaks `is` identity checks used by cascade tests (S179
        # T1/T2) and could bite anywhere else that compares handler identity.
        # Sections 80/107/112 work around this with a function sentinel; this
        # approach is simpler and applies globally.
        self._on_finished = self._on_finished
        self._cleanup_stale_recovery_files()
        try:
            self._recover_crashed_cascade_runs()
        # broadly defensive — crashed-run recovery probe at startup; any error falls through to a normal boot rather than blocking app start.
        except Exception as exc:
            print(f"Cascade crash recovery failed: {exc}", file=sys.stderr)

        # Restore geometry
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        geo = self.settings.value("window/geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)

        # Drag and drop
        self.setAcceptDrops(True)

        # Stacked central area: picker (index 0) and form container (index 1)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Picker
        self.picker = ToolPicker()
        self.picker.tool_selected.connect(self._load_tool_path)
        self.picker.back_requested.connect(self._on_picker_back)
        self.stack.addWidget(self.picker)

        # Form container (built on load)
        self.form_container = QWidget()
        self.form_container_layout = QVBoxLayout(self.form_container)
        self.form_container_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.form_container)

        # Status bar
        if _check_already_elevated():
            self.statusBar().showMessage("Ready — Running as administrator")
        else:
            self.statusBar().showMessage("Ready")

        # Progress label on the status bar's permanent (right-aligned) side.
        # Progress text ("Running... (Ns)") goes here so it doesn't stomp
        # sticky main-region messages like "Loaded X — N fields".
        self._progress_label = QLabel("")
        self.statusBar().addPermanentWidget(self._progress_label)

        # Cascade sidebar (created before _build_menu so menu can reference it)
        self.cascade_dock = CascadeSidebar(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.cascade_dock)

        self._build_menu()
        self._build_shortcuts()

        visible = self.settings.value("cascade/visible", False, type=bool)
        self.cascade_dock.setVisible(visible)
        self.act_cascade_toggle.setChecked(visible)
        if visible:
            self.setMinimumWidth(MIN_WINDOW_WIDTH + 320)
        self.cascade_dock.visibilityChanged.connect(
            self._on_cascade_visibility_changed
        )

        if tool_path:
            self._load_tool_path(tool_path)
        else:
            # Try to reopen last tool
            last = self.settings.value("session/last_tool")
            if last and Path(last).exists():
                self._load_tool_path(last)
            else:
                self._show_picker()

        # First-run welcome: schedule after the window is fully painted.
        # _maybe_show_welcome re-checks the flag at fire time.
        if (not type(self)._suppress_welcome_dialog
                and int(self.settings.value("picker/welcome_dismissed", 0) or 0) != 1):
            QTimer.singleShot(0, self._maybe_show_welcome)

    def _maybe_show_welcome(self) -> None:
        """Show the WelcomeDialog if the dismissed flag is still false."""
        if int(self.settings.value("picker/welcome_dismissed", 0) or 0) == 1:
            return
        WelcomeDialog(self).exec()

    def _build_menu(self) -> None:
        """Build the menu bar with File, Presets, and View menus."""
        menu = self.menuBar().addMenu("File")

        self.act_load = menu.addAction("Load Tool...")
        self.act_load.setShortcut("Ctrl+O")
        self.act_load.triggered.connect(self._on_load_file)

        self.act_reload = menu.addAction("Reload Tool")
        self.act_reload.setShortcut("Ctrl+R")
        self.act_reload.triggered.connect(self._on_reload)
        self.act_reload.setEnabled(False)

        self.act_back = menu.addAction("Tool List")
        self.act_back.setShortcut("Ctrl+B")
        self.act_back.triggered.connect(self._on_back)
        self.act_back.setEnabled(False)

        menu.addSeparator()

        self.act_custom_paths = menu.addAction("Custom PATH Directories...")
        self.act_custom_paths.triggered.connect(self._on_custom_paths)

        self.act_reset_pw_copy = menu.addAction("Reset password copy prompt")
        self.act_reset_pw_copy.triggered.connect(self._on_reset_pw_copy_prompt)

        menu.addSeparator()

        act_exit = menu.addAction("Exit")
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.close)

        # Presets menu (enabled when a tool is loaded)
        self.preset_menu = self.menuBar().addMenu("Presets")
        self.preset_menu.setEnabled(False)

        self.act_save_preset = self.preset_menu.addAction("Save Preset...")
        self.act_save_preset.setShortcut("Ctrl+S")
        self.act_save_preset.triggered.connect(self._on_save_preset)

        self.act_load_preset = self.preset_menu.addAction("Preset List...")
        self.act_load_preset.setShortcut("Ctrl+L")
        self.act_load_preset.triggered.connect(self._on_load_preset)

        self.act_edit_preset = self.preset_menu.addAction("Edit Preset...")
        self.act_edit_preset.triggered.connect(self._on_edit_preset)

        self.preset_menu.addSeparator()

        self.act_import_preset = self.preset_menu.addAction("Import Preset...")
        self.act_import_preset.triggered.connect(self._on_import_preset)

        self.act_export_preset = self.preset_menu.addAction("Export Preset...")
        self.act_export_preset.triggered.connect(self._on_export_preset)

        # Cascade menu
        cascade_menu = self.menuBar().addMenu("Cascade")
        self.act_cascade_toggle = cascade_menu.addAction("Show Cascade Panel")
        self.act_cascade_toggle.setShortcut("Ctrl+G")
        self.act_cascade_toggle.setCheckable(True)
        self.act_cascade_toggle.triggered.connect(self._toggle_cascade)

        cascade_menu.addSeparator()
        self.act_save_cascade = cascade_menu.addAction("Save Cascade...")
        self.act_save_cascade.triggered.connect(self.cascade_dock._on_save_cascade_file)
        self.act_load_cascade = cascade_menu.addAction("Cascade List...")
        self.act_load_cascade.triggered.connect(self.cascade_dock._on_load_cascade_list)

        cascade_menu.addSeparator()
        self.act_import_cascade = cascade_menu.addAction("Import Cascade...")
        self.act_import_cascade.triggered.connect(self.cascade_dock._on_import_cascade)
        self.act_export_cascade = cascade_menu.addAction("Export Cascade...")
        self.act_export_cascade.triggered.connect(self.cascade_dock._on_export_cascade)

        # View menu — theme selector
        view_menu = self.menuBar().addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.act_theme_light = QAction("Light", self, checkable=True)
        self.act_theme_light.setData("light")
        self.act_theme_dark = QAction("Dark", self, checkable=True)
        self.act_theme_dark.setData("dark")
        self.act_theme_system = QAction("System Default", self, checkable=True)
        self.act_theme_system.setData("system")
        for act in (self.act_theme_light, self.act_theme_dark, self.act_theme_system):
            theme_group.addAction(act)
            theme_menu.addAction(act)
        theme_group.triggered.connect(lambda act: self._set_theme(act.data()))
        self._sync_theme_checks()
        # Shortcut to toggle between light/dark
        toggle_shortcut = QShortcut(QKeySequence("Ctrl+D"), self)
        toggle_shortcut.activated.connect(self._toggle_dark_mode)

        view_menu.addSeparator()
        self.act_history = view_menu.addAction("History...")
        self.act_history.setShortcut(QKeySequence("Ctrl+H"))
        self.act_history.triggered.connect(self._on_show_history)
        self.act_history.setEnabled(False)

        self.act_cascade_history = view_menu.addAction("Cascade History...")
        self.act_cascade_history.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.act_cascade_history.triggered.connect(self._on_show_cascade_history)

        # Help menu
        help_menu = self.menuBar().addMenu("Help")
        act_about = help_menu.addAction("About Scaffold")
        act_about.triggered.connect(self._on_about)
        act_user_guide = help_menu.addAction("User Guide")
        act_user_guide.triggered.connect(self._on_show_user_guide)
        act_cascade_guide = help_menu.addAction("Cascade Guide")
        act_cascade_guide.triggered.connect(self._on_show_cascade_guide)
        act_shortcuts = help_menu.addAction("Keyboard Shortcuts")
        act_shortcuts.triggered.connect(self._on_keyboard_shortcuts)

        self.menuBar().setStyleSheet("QMenu::item { padding: 4px 28px 4px 20px; }")

    def _sync_theme_checks(self) -> None:
        """Update View > Theme check marks to match the stored preference."""
        pref = self.settings.value("appearance/theme", "system")
        self.act_theme_light.setChecked(pref == "light")
        self.act_theme_dark.setChecked(pref == "dark")
        self.act_theme_system.setChecked(pref == "system")

    def _set_theme(self, pref: str) -> None:
        """Persist theme preference and apply it immediately."""
        self.settings.setValue("appearance/theme", pref)
        if pref == "dark":
            apply_theme(True)
        elif pref == "light":
            apply_theme(False)
        else:
            apply_theme(_detect_system_dark())
        self._apply_widget_theme()

    def _toggle_dark_mode(self) -> None:
        """Toggle between dark and light themes (Ctrl+D)."""
        self._set_theme("dark" if not _dark_mode else "light")
        self._sync_theme_checks()

    def _apply_cascade_minwidth(self, visible: bool) -> None:
        """Set the window minimum width based on cascade dock visibility."""
        if visible:
            min_needed = MIN_WINDOW_WIDTH + 320
            if self.width() < min_needed:
                self.resize(min_needed, self.height())
            self.setMinimumWidth(min_needed)
        else:
            self.setMinimumWidth(MIN_WINDOW_WIDTH)

    def _on_cascade_visibility_changed(self, visible: bool) -> None:
        """React to cascade dock visibility changes (native X, menu, or programmatic)."""
        self._apply_cascade_minwidth(visible)
        self.act_cascade_toggle.setChecked(visible)
        self.settings.setValue("cascade/visible", visible)

    def _toggle_cascade(self, checked: bool = None) -> None:
        """Show/hide the Cascade sidebar and persist the state."""
        if checked is None:
            checked = not self.cascade_dock.isVisible()
        self._apply_cascade_minwidth(checked)
        self.cascade_dock.setVisible(checked)
        self.act_cascade_toggle.setChecked(checked)
        self.settings.setValue("cascade/visible", checked)

    def _on_about(self) -> None:
        """Show the About Scaffold dialog."""
        QMessageBox.about(
            self,
            "About Scaffold",
            f"<b>Scaffold {__version__}</b><br><br>"
            "Dynamic GUI form generator for CLI tools.<br>"
            "License: PolyForm Noncommercial 1.0.0<br><br>"
            "<a href='https://github.com/kevwillow/scaffold'>github.com/kevwillow/scaffold</a>",
        )

    def _on_show_user_guide(self) -> None:
        """Show the User Guide dialog covering core app features."""
        dlg = QDialog(self)
        dlg.setWindowTitle("User Guide")
        dlg.setMinimumWidth(600)
        dlg.resize(640, 560)
        layout = QVBoxLayout(dlg)

        content = QLabel()
        content.setTextFormat(Qt.TextFormat.RichText)
        content.setWordWrap(True)
        content.setText(
            "<h2>Scaffold User Guide</h2>"

            "<h3>What is Scaffold?</h3>"
            "<p>Scaffold is a GUI wrapper for command-line tools. It reads a JSON "
            "schema describing a tool's flags and arguments, builds a form for "
            "those fields, assembles the resulting command, and runs it.</p>"

            "<h3>Loading a Tool</h3>"
            "<p>Open a tool with File \u2192 Load Tool, drag a .json schema onto "
            "the window, or browse the bundled set with Tool List (Ctrl+B).</p>"

            "<h3>The Form</h3>"
            "<p>Field types come from the schema. Required fields are marked "
            "with a red asterisk. Find Field (Ctrl+F) filters the form by name. "
            "Reset to Defaults reverts every field to its schema default.</p>"

            "<h3>Command Preview</h3>"
            "<p>The bottom of the window shows the assembled command in real "
            "time. Copy Command places it on the clipboard. On Windows, a "
            "CMD/PowerShell dropdown switches the quoting style for the copied "
            "command.</p>"

            "<h3>Running</h3>"
            "<p>Run with the Run button, Ctrl+Enter, or bare Enter when focus "
            "is outside a text field. Stop or Escape cancels a running process. "
            "The Timeout spinner sets an auto-kill window. The output panel "
            "color-codes stdout and stderr.</p>"

            "<h3>Presets</h3>"
            "<p>Save the current field values with Ctrl+S; reopen with Ctrl+L. "
            "Presets are stored per-tool. Descriptions are editable from the "
            "Preset List dialog. Presets can be imported and exported as JSON.</p>"

            "<h3>History</h3>"
            "<p>View \u2192 History (Ctrl+H) lists previous runs of the current "
            "tool. Clicking an entry restores that run's preset into the form. "
            "History can be exported to JSON.</p>"

            "<h3>Creating New Schemas</h3>"
            "<p>Place new JSON schema files in the <code>tools/</code> folder. "
            "Schemas can be hand-written \u2014 schema.md documents the full "
            "format. An LLM is not required, it just makes the work faster: "
            "SCHEMA_PROMPT.txt contains a prompt that turns a tool's help text into "
            "a schema. Either way, validate the result with "
            "<code>python scaffold.py --validate path/to/tool.json</code>.</p>"

            "<h3>Cascades</h3>"
            "<p>Cascades chain multiple tools to run in sequence \u2014 see "
            "Help \u2192 Cascade Guide for details.</p>"

            "<h3>Themes</h3>"
            "<p>Switch theme from View \u2192 Theme, or toggle dark/light with "
            "Ctrl+D.</p>"
        )

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        if _dark_mode:
            dlg.setStyleSheet(
                f"QDialog {{ background-color: {DARK_COLORS['widget']}; color: {DARK_COLORS['text']}; }}"
            )

        dlg.exec()

    def _on_show_cascade_guide(self) -> None:
        """Show the Cascade Guide dialog explaining cascade features."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Cascade Guide")
        dlg.setMinimumWidth(600)
        dlg.resize(640, 520)
        layout = QVBoxLayout(dlg)

        content = QLabel()
        content.setTextFormat(Qt.TextFormat.RichText)
        content.setWordWrap(True)
        content.setText(
            "<h2>What is a Cascade?</h2>"
            "<p>A cascade chains multiple tools together to run in sequence. Each step "
            "uses its own tool and preset configuration. Steps run one after another, but "
            "the output of one step is <b>not</b> automatically fed into the next — each "
            "step operates independently with its own configured arguments.</p>"

            "<h3>Slots</h3>"
            "<p>The cascade panel shows up to 20 numbered slots. To add a tool to a slot, "
            "click an empty slot button or right-click and choose from available tools. Each "
            "slot can optionally have a preset applied. To clear a slot, click the arrow "
            "button next to it and select the clear option, or click the remove button.</p>"

            "<h3>Delays</h3>"
            "<p>Each slot has a delay spinner (in seconds) that controls how long the "
            "cascade waits after that step completes before starting the next one. "
            "Set to 0 for no delay.</p>"

            "<h3>Loop Mode</h3>"
            "<p>The Loop button makes the cascade repeat the entire chain continuously "
            "until you stop it. After the last step finishes, the chain restarts from "
            "the first step. This is useful for monitoring or repeated batch operations.</p>"

            "<h3>Stop on Error</h3>"
            "<p>When enabled, the cascade halts if any step exits with a non-zero exit "
            "code. Timed-out steps count as errors. When disabled, the cascade continues "
            "to the next step regardless of exit codes.</p>"

            "<h3>Pause / Resume</h3>"
            "<p>Clicking Pause requests the cascade to pause after the <b>current</b> step "
            "finishes — it does not interrupt a running step mid-execution. The button "
            "changes to Resume so you can continue. The Stop button and Clear All both "
            "fully reset the cascade state.</p>"

            "<h3>Cascade Variables</h3>"
            "<p>Variables are runtime values prompted before the chain runs. Each variable "
            "has a name, a target flag, and an optional scope (all slots or specific slot "
            "numbers). When the chain starts, you enter values for each variable, and they "
            "are injected into matching field flags across the selected slots. Use the "
            "Edit Variables button in the cascade panel to define them.</p>"

            "<h3>Save / Load / Import / Export</h3>"
            "<p>Named cascade configurations can be saved and loaded from the cascade "
            "menu. Import and export allow sharing cascade files between machines or "
            "users.</p>"

            "<h3>History</h3>"
            "<p>Cascade runs are recorded automatically. Open the history dialog from "
            "View \u2192 Cascade History or Ctrl+Shift+H to browse past runs, restore "
            "individual steps or full cascades, and export history to JSON. In loop "
            "mode, each iteration is a separate entry, so loop-heavy runs fill "
            "history faster.</p>"
        )

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        if _dark_mode:
            dlg.setStyleSheet(
                f"QDialog {{ background-color: {DARK_COLORS['widget']}; color: {DARK_COLORS['text']}; }}"
            )

        dlg.exec()

    def _on_keyboard_shortcuts(self) -> None:
        """Show a list of keyboard shortcuts."""
        shortcuts = (
            "<pre style='font-family: Consolas, Menlo, monospace;"
            " font-size: 10pt; margin: 0; padding: 6px 16px 6px 6px;'>"
            "<b>File &amp; Tools</b>\n"
            "  Ctrl+O          Load Tool...\n"
            "  Ctrl+R          Reload Tool\n"
            "  Ctrl+B          Tool List\n"
            "  Ctrl+Q          Exit\n"
            "\n"
            "<b>Presets &amp; History</b>\n"
            "  Ctrl+S          Save Preset...\n"
            "  Ctrl+L          Preset List...\n"
            "  Ctrl+H          History...\n"
            "  Ctrl+Shift+H    Cascade History...\n"
            "\n"
            "<b>View</b>\n"
            "  Ctrl+D          Toggle Dark/Light Theme\n"
            "  Ctrl+G          Toggle Cascade Panel\n"
            "  Ctrl+F          Find Field\n"
            "  Ctrl+Shift+F    Search Output\n"
            "\n"
            "<b>Execution</b>\n"
            "  Ctrl+Enter      Run Command\n"
            "  Enter           Run Command (when not typing)\n"
            "  Shift+Enter     Run Cascade (when not typing)\n"
            "  Escape          Stop Process / Close Search"
            "</pre>"
        )
        QMessageBox.information(self, "Keyboard Shortcuts", shortcuts)

    def _apply_widget_theme(self) -> None:
        """Re-apply theme-sensitive inline stylesheets for output panel, preview bar, and warning bar."""
        # Output panel
        if hasattr(self, "output"):
            if _dark_mode:
                self.output.setStyleSheet(
                    f"QPlainTextEdit {{ background-color: {DARK_COLORS['output_bg']};"
                    f" color: {DARK_COLORS['output_text']}; }}"
                )
            else:
                self.output.setStyleSheet(
                    f"QPlainTextEdit {{ background-color: {OUTPUT_BG}; color: {OUTPUT_FG}; }}"
                )
        # Preview bar
        if hasattr(self, "preview"):
            if _dark_mode:
                self.preview.setStyleSheet(
                    f"QTextEdit {{ background-color: {DARK_COLORS['widget']};"
                    f" color: {DARK_COLORS['text']}; }}"
                )
            else:
                self.preview.setStyleSheet("")
            # Re-color preview with new theme colors
            if hasattr(self, "form") and self.form:
                self._update_preview()
        # Section labels and separators
        for attr in ("preview_label", "output_label"):
            if hasattr(self, attr):
                self._style_section_label(getattr(self, attr))
        if hasattr(self, "preview_label"):
            self.preview_label.setStyleSheet(
                self.preview_label.styleSheet().replace("2px 0 1px 0;", "0px 0 0px 0;"))
        # Form frame border
        if hasattr(self, "form_frame"):
            self._style_form_frame()
        # Drag handle
        if hasattr(self, "output_handle"):
            self.output_handle.update()
        # Run button
        if hasattr(self, "run_btn"):
            self._style_run_btn()
        # Warning bar
        if hasattr(self, "warning_bar") and self.warning_bar.isVisible():
            self._style_warning_bar()
            if self.data:
                self._set_warning_bar_text(self.data.get("binary", ""))
        # Update required label colors in the form
        if hasattr(self, "form"):
            self.form.update_theme()
        # Force repaint — must happen BEFORE cascade theme update so that
        # setStyle() doesn't reset the inline styles that update_theme() applies.
        QApplication.instance().setStyle(QApplication.instance().style().name())
        # Cascade sidebar
        if hasattr(self, "cascade_dock"):
            self.cascade_dock.update_theme()

    @staticmethod
    def _style_section_label(label: QLabel) -> None:
        """Apply theme-appropriate styling to a section header label."""
        color = DARK_COLORS["text_dim"] if _dark_mode else "#666666"
        label.setStyleSheet(
            "font-weight: bold; font-size: 11px; text-transform: uppercase;"
            f" letter-spacing: 1px; padding: 2px 0 1px 0; color: {color};"
        )


    def _style_form_frame(self) -> None:
        """Apply theme-appropriate border styling to the command options frame."""
        self.form_frame.setObjectName("form_frame")
        if _dark_mode:
            self.form_frame.setStyleSheet(
                f"QFrame#form_frame {{ border: 1px solid {DARK_COLORS['border']};"
                f" border-radius: 4px; background-color: {DARK_COLORS['window']}; }}"
            )
        else:
            self.form_frame.setStyleSheet(
                "QFrame#form_frame { border: 1px solid #999999;"
                " border-radius: 4px; }"
            )

    def _style_run_btn(self) -> None:
        """Apply green (Run), red (Stop), or amber (Stopping...) styling to the run button."""
        text = self.run_btn.text()
        if _dark_mode:
            if text == "Stop":
                self.run_btn.setStyleSheet(
                    f"QPushButton {{ background-color: #45272a; color: {DARK_COLORS['error']};"
                    f" border: 1px solid {DARK_COLORS['error']}; padding: 4px 16px;"
                    f" font-weight: bold; border-radius: 3px; }} "
                    f"QPushButton:hover {{ background-color: #5a3035; }}"
                )
            elif text == "Stopping...":
                self.run_btn.setStyleSheet(
                    f"QPushButton {{ background-color: #3a3020; color: {COLOR_WARN};"
                    f" border: 1px solid {COLOR_WARN}; padding: 4px 16px;"
                    f" font-style: italic; border-radius: 3px; }} "
                    f"QPushButton:disabled {{ background-color: #3a3020; color: {COLOR_WARN};"
                    f" border: 1px solid {COLOR_WARN}; padding: 4px 16px;"
                    f" font-style: italic; border-radius: 3px; }}"
                )
            else:
                self.run_btn.setStyleSheet(
                    f"QPushButton {{ background-color: #1e3a2a; color: {DARK_COLORS['success']};"
                    f" border: 1px solid {DARK_COLORS['success']}; padding: 4px 16px;"
                    f" font-weight: bold; border-radius: 3px; }} "
                    f"QPushButton:hover {{ background-color: #274d36; }}"
                )
        else:
            if text == "Stop":
                self.run_btn.setStyleSheet(
                    "QPushButton { background-color: #fdecea; color: #c62828;"
                    " border: 1px solid #ef9a9a; padding: 4px 16px;"
                    " font-weight: bold; border-radius: 3px; } "
                    "QPushButton:hover { background-color: #f9d0cd; }"
                )
            elif text == "Stopping...":
                self.run_btn.setStyleSheet(
                    f"QPushButton {{ background-color: #fdf5e6; color: #b8860b;"
                    f" border: 1px solid {COLOR_WARN}; padding: 4px 16px;"
                    f" font-style: italic; border-radius: 3px; }} "
                    f"QPushButton:disabled {{ background-color: #fdf5e6; color: #b8860b;"
                    f" border: 1px solid {COLOR_WARN}; padding: 4px 16px;"
                    f" font-style: italic; border-radius: 3px; }}"
                )
            else:
                self.run_btn.setStyleSheet(
                    "QPushButton { background-color: #e8f5e9; color: #2e7d32;"
                    " border: 1px solid #a5d6a7; padding: 4px 16px;"
                    " font-weight: bold; border-radius: 3px; } "
                    "QPushButton:hover { background-color: #c8e6c9; }"
                )

    def _style_warning_bar(self) -> None:
        """Apply theme-appropriate styling to the binary-not-found warning bar."""
        if _dark_mode:
            self.warning_bar.setStyleSheet(
                f"background-color: {DARK_COLORS['warning_bg']};"
                f" color: {DARK_COLORS['warning_text']};"
                f" padding: 6px 12px;"
                f" border: 1px solid {DARK_COLORS['warning_border']};"
                " border-radius: 4px;"
                " font-weight: bold;"
            )
        else:
            self.warning_bar.setStyleSheet(
                f"background-color: {LIGHT_WARNING_BG}; color: {LIGHT_WARNING_FG};"
                f" padding: 6px 12px; border: 1px solid {LIGHT_WARNING_BORDER};"
                " border-radius: 4px;"
                " font-weight: bold;"
            )

    def _set_warning_bar_text(self, binary: str) -> None:
        """Set warning bar rich text with a clickable link to the custom PATH dialog."""
        link_color = DARK_COLORS["accent"] if _dark_mode else "#0550ae"
        escaped = html.escape(binary)
        self.warning_bar.setText(
            f"Warning: '{escaped}' not found in PATH &mdash; "
            f"<a href='#' style='color:{link_color}'>Configure custom directories</a>"
        )

    def _build_shortcuts(self) -> None:
        """Register global keyboard shortcuts for Run (Ctrl+Enter), Stop (Escape), Find (Ctrl+F), Output Search (Ctrl+Shift+F)."""
        run_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        run_shortcut.activated.connect(self._shortcut_run)
        stop_shortcut = QShortcut(QKeySequence("Escape"), self)
        stop_shortcut.activated.connect(self._shortcut_stop)
        find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        find_shortcut.activated.connect(self._shortcut_find)
        output_find_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        output_find_shortcut.activated.connect(self._shortcut_output_find)

        # Bare Enter — run command (suppressed when typing in text inputs)
        self._shortcut_run_enter = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        self._shortcut_run_enter.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_run_enter.activated.connect(self._shortcut_run_if_safe)

        self._shortcut_run_enter_num = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        self._shortcut_run_enter_num.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_run_enter_num.activated.connect(self._shortcut_run_if_safe)

        # Shift+Enter — run cascade (suppressed when typing in text inputs)
        self._shortcut_run_cascade = QShortcut(QKeySequence("Shift+Return"), self)
        self._shortcut_run_cascade.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_run_cascade.activated.connect(self._shortcut_run_cascade_if_safe)

        self._shortcut_run_cascade_num = QShortcut(QKeySequence("Shift+Enter"), self)
        self._shortcut_run_cascade_num.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._shortcut_run_cascade_num.activated.connect(self._shortcut_run_cascade_if_safe)

        # Cascade control shortcuts — operate on cascade state regardless of
        # sidebar visibility.
        self._shortcut_cascade_add = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self._shortcut_cascade_add.activated.connect(self.cascade_dock._on_add_slot)
        self._shortcut_cascade_pause = QShortcut(QKeySequence("Ctrl+P"), self)
        self._shortcut_cascade_pause.activated.connect(self.cascade_dock._on_pause_chain)
        self._shortcut_cascade_stop = QShortcut(QKeySequence("Ctrl+."), self)
        self._shortcut_cascade_stop.activated.connect(self.cascade_dock._on_stop_chain)
        self._shortcut_cascade_loop = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        self._shortcut_cascade_loop.activated.connect(self.cascade_dock._toggle_loop)
        self._shortcut_cascade_err = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self._shortcut_cascade_err.activated.connect(self.cascade_dock._toggle_stop_on_error)

    def _focus_is_in_text_input(self) -> bool:
        """Return True if the currently focused widget is a text input that should consume Enter."""
        fw = QApplication.focusWidget()
        if fw is None:
            return False
        if isinstance(fw, QComboBox):
            return fw.isEditable()
        return isinstance(fw, (QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox))

    def _shortcut_run_if_safe(self) -> None:
        """Trigger Run via bare Enter when focus is not in a text input and no process is running."""
        if self._focus_is_in_text_input():
            return
        if self.data and self.stack.currentIndex() == 1:
            if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
                self._on_run_stop()

    def _shortcut_run_cascade_if_safe(self) -> None:
        """Trigger cascade Run via Shift+Enter when focus is not in a text input."""
        if self._focus_is_in_text_input():
            return
        if not hasattr(self, 'cascade_dock') or not self.cascade_dock.isVisible():
            return
        # Only fire if at least one slot has a valid tool assigned
        has_slot = any(
            s.get("tool_path") and Path(s["tool_path"]).exists()
            for s in self.cascade_dock._slots
        )
        if has_slot:
            self.cascade_dock._on_run_chain()

    def _shortcut_run(self) -> None:
        """Trigger Run via Ctrl+Enter when the form view is active and no process is running."""
        if self.data and self.stack.currentIndex() == 1:
            if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
                self._on_run_stop()

    def _shortcut_stop(self) -> None:
        """Trigger Stop via Escape — close output search first, then field search, then stop process."""
        if hasattr(self, '_output_search_widget') and self._output_search_widget.isVisible() and self._output_search_bar.hasFocus():
            self._close_output_search()
            return
        if hasattr(self, 'form') and self.form and self.form._search_row_widget.isVisible():
            self.form.close_search()
            return
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self._on_run_stop()

    def _shortcut_find(self) -> None:
        """Open field search bar via Ctrl+F when form view is active."""
        if hasattr(self, 'form') and self.form and self.stack.currentIndex() == 1:
            self.form.open_search()

    def _shortcut_output_find(self) -> None:
        """Open output search bar via Ctrl+Shift+F when form view is active."""
        if self.stack.currentIndex() == 1 and hasattr(self, '_output_search_widget'):
            self._output_search_widget.setVisible(True)
            self._output_search_bar.setFocus()
            self._output_search_bar.selectAll()

    # ------------------------------------------------------------------
    # Output search (Ctrl+Shift+F)
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        """Handle key events on the output search bar for Enter/Shift+Enter/Escape."""
        if obj is self._output_search_bar and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._output_search_prev()
                else:
                    self._output_search_next()
                return True
            if key == Qt.Key.Key_Escape:
                self._close_output_search()
                return True
        return super().eventFilter(obj, event)

    def _update_output_search_visibility(self) -> None:
        """Show the output search bar when output has content, hide when empty."""
        has_content = bool(self.output.toPlainText().strip())
        self._output_search_widget.setVisible(has_content)
        if not has_content:
            self._close_output_search()

    def _close_output_search(self) -> None:
        """Clear search state/highlights. Only hide the widget if output is empty."""
        self._output_search_bar.clear()
        has_content = bool(self.output.toPlainText().strip())
        if not has_content:
            self._output_search_widget.setVisible(False)
        self._output_search_matches = []
        self._output_search_index = -1
        self._output_search_count_label.setText("")
        self.output.setExtraSelections([])
        self._output_search_bar.clearFocus()

    def _on_output_search_changed(self, text: str) -> None:
        """Recompute all matches and highlight them when search text changes."""
        self._output_search_matches = []
        self._output_search_index = -1
        query = text.strip()
        if not query:
            self._output_search_count_label.setText("")
            self.output.setExtraSelections([])
            return

        # Find all matches (case-insensitive)
        doc = self.output.document()
        cursor = QTextCursor(doc)
        flags = doc.FindFlag(0)  # no flags = forward, case-insensitive by default
        # QTextDocument.find is case-insensitive by default when no FindCaseSensitively flag is set
        while True:
            cursor = doc.find(query, cursor)
            if cursor.isNull():
                break
            start = cursor.selectionStart()
            length = cursor.selectionEnd() - start
            self._output_search_matches.append((start, length))

        if self._output_search_matches:
            self._output_search_index = 0
            self._output_search_count_label.setText(
                f"1 of {len(self._output_search_matches)}"
            )
        else:
            self._output_search_count_label.setText("0 matches")

        self._apply_output_search_highlights()

    def _apply_output_search_highlights(self) -> None:
        """Apply extraSelections to highlight all matches, current match in orange."""
        selections = []

        # Yellow for all matches
        yellow_fmt = QTextCharFormat()
        yellow_fmt.setBackground(QColor("#fff176"))
        yellow_fmt.setForeground(QColor("#000000"))

        # Orange for current match
        orange_fmt = QTextCharFormat()
        orange_fmt.setBackground(QColor("#ff9800"))
        orange_fmt.setForeground(QColor("#000000"))

        for i, (start, length) in enumerate(self._output_search_matches):
            sel = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.output.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = cursor
            sel.format = orange_fmt if i == self._output_search_index else yellow_fmt
            selections.append(sel)

        self.output.setExtraSelections(selections)

        # Scroll to current match
        if self._output_search_index >= 0 and self._output_search_matches:
            start, length = self._output_search_matches[self._output_search_index]
            cursor = QTextCursor(self.output.document())
            cursor.setPosition(start)
            self.output.setTextCursor(cursor)
            self.output.ensureCursorVisible()

    def _output_search_next(self) -> None:
        """Jump to the next output search match (Enter key)."""
        if not self._output_search_matches:
            return
        self._output_search_index = (self._output_search_index + 1) % len(self._output_search_matches)
        total = len(self._output_search_matches)
        self._output_search_count_label.setText(
            f"{self._output_search_index + 1} of {total}"
        )
        self._apply_output_search_highlights()

    def _output_search_prev(self) -> None:
        """Jump to the previous output search match (Shift+Enter)."""
        if not self._output_search_matches:
            return
        self._output_search_index = (self._output_search_index - 1) % len(self._output_search_matches)
        total = len(self._output_search_matches)
        self._output_search_count_label.setText(
            f"{self._output_search_index + 1} of {total}"
        )
        self._apply_output_search_highlights()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _clamp_output_height(self) -> None:
        """Ensure the output panel height fits within the current window."""
        max_h = self.output_handle._effective_max_height()
        cur_h = self.output.height()
        if cur_h > max_h:
            clamped = max(OUTPUT_MIN_HEIGHT, max_h)
            self.output.setFixedHeight(clamped)
            self.settings.setValue("output/height", clamped)

    def resizeEvent(self, event) -> None:
        """Clamp output panel height when the window shrinks."""
        super().resizeEvent(event)
        if hasattr(self, "output"):
            self._clamp_output_height()

    def showEvent(self, event) -> None:
        """Clamp restored output height against actual window size on first show."""
        super().showEvent(event)
        if hasattr(self, "output"):
            self._clamp_output_height()

    # ------------------------------------------------------------------
    # Auto-save / crash recovery
    # ------------------------------------------------------------------

    def _cleanup_stale_recovery_files(self) -> None:
        """Delete expired recovery files from previous sessions."""
        tmp = Path(tempfile.gettempdir())
        for f in tmp.glob(f"scaffold_recovery_{_user_id()}_*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                ts = data.get("_recovery_timestamp", 0)
                # Defensive: non-numeric (or bool) timestamp — treat as stale.
                # isinstance(True, int) is True in Python, so check bool first.
                if type(ts) is bool or not isinstance(ts, (int, float)):
                    f.unlink()
                    continue
                if (time.time() - ts) > AUTOSAVE_EXPIRY_HOURS * 3600:
                    f.unlink()
            except (OSError, json.JSONDecodeError):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _recovery_file_path(self) -> Path | None:
        """Return the recovery file path for the current tool, or None."""
        if self.data is None:
            return None
        safe_name = _sanitize_filename_component(self.data["tool"])
        return Path(tempfile.gettempdir()) / f"scaffold_recovery_{_user_id()}_{safe_name}.json"

    def _autosave_on_change(self) -> None:
        """Restart the debounce timer on every field change."""
        if self.data is None:
            return
        self._autosave_timer.start()

    def _autosave_form(self) -> None:
        """Serialize form state to a recovery file after the debounce delay."""
        if self.form is None or self.data is None:
            return
        values = self.form.serialize_values()
        # Don't save if form matches default state (user hasn't changed anything)
        if self._default_form_snapshot is not None:
            current = {k: v for k, v in values.items() if not k.startswith("_")}
            default = {k: v for k, v in self._default_form_snapshot.items() if not k.startswith("_")}
            if current == default:
                return
        # Don't save if only meta keys are present (form is at defaults/empty)
        if not any(k for k in values if not k.startswith("_")):
            return
        values["_recovery_tool_path"] = self.tool_path
        values["_recovery_timestamp"] = time.time()
        path = self._recovery_file_path()
        if path is None:
            return
        try:
            _atomic_write_json(path, values)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            if not self._autosave_warned:
                self._autosave_warned = True
                self.statusBar().showMessage(
                    f"Auto-save failed \u2014 crash recovery unavailable ({exc.strerror or 'I/O error'})",
                    10000,
                )
            print(f"[scaffold] autosave write failed: {exc}", file=sys.stderr)

    def _clear_recovery_file(self) -> None:
        """Delete the recovery file if it exists."""
        path = self._recovery_file_path()
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _check_for_recovery(self) -> None:
        """On tool load, offer to restore auto-saved form state."""
        if self._chain_preserve_output:
            return
        path = self._recovery_file_path()
        if path is None or not path.exists():
            return
        try:
            recovery_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if not isinstance(recovery_data, dict):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        ts = recovery_data.get("_recovery_timestamp", 0)
        # Defensive: non-numeric (or bool) timestamp — treat as corrupt,
        # matching the JSONDecodeError branch above. isinstance(True, int) is
        # True in Python, so check bool first.
        if type(ts) is bool or not isinstance(ts, (int, float)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if (time.time() - ts) > AUTOSAVE_EXPIRY_HOURS * 3600:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if recovery_data.get("_recovery_tool_path") != self.tool_path:
            # Recovery file exists but was saved for this tool at a different
            # path (tool file was moved/copied).  Discard it — the form state
            # may not match the current schema.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._show_status(
                "Recovery file discarded \u2014 tool location has changed"
            )
            return
        human_time = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        btn = QMessageBox.question(
            self, "Recover Unsaved Work",
            f"An auto-saved form state was found from {human_time}.\n\n"
            "Would you like to restore it?",
        )
        if btn == QMessageBox.StandardButton.Yes:
            had_pw = self.form.apply_values(recovery_data)
            self._default_form_snapshot = self.form.serialize_values()
            if had_pw:
                self.statusBar().showMessage(
                    "Recovery file loaded — password fields were not saved and must be re-entered."
                )
            else:
                self.statusBar().showMessage("Form restored from auto-save")
        else:
            self.statusBar().showMessage("Recovery discarded")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def closeEvent(self, event) -> None:
        """Persist window geometry and session state; kill any running process on exit."""
        # Stop chain runner if active
        if self.cascade_dock._chain_state != CHAIN_IDLE:
            self.cascade_dock._chain_cleanup("Closing", silent=True)
        # Stop timers FIRST to prevent them from recreating files during teardown
        self._autosave_timer.stop()
        self._elapsed_timer.stop()
        self._clear_recovery_file()
        self.settings.setValue("window/geometry", self.saveGeometry())
        if self.tool_path:
            self.settings.setValue("session/last_tool", self.tool_path)
        # Kill any running process
        self._teardown_process()
        event.accept()

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept drag events that contain at least one .json file URL."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".json"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """Route the first dropped .json file to its handler based on _format."""
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not path.lower().endswith(".json"):
                continue
            try:
                data = _read_user_json(path)
            except RuntimeError as e:
                self.statusBar().showMessage(f"Error loading file: {e}")
                return
            except (json.JSONDecodeError, OSError) as e:
                self.statusBar().showMessage(f"Error loading file: {e}")
                return
            fmt = data.get("_format") if isinstance(data, dict) else None
            if fmt == "scaffold_preset":
                if self.data is None:
                    QMessageBox.information(
                        self, "Open a Tool First",
                        "Open a tool first, then drop a preset to apply it.",
                    )
                    return
                self._apply_preset_from_path(path)
                return
            if fmt == "scaffold_cascade":
                dock = self.cascade_dock
                has_any = any(s.get("tool_path") for s in dock._slots)
                if has_any:
                    answer = QMessageBox.question(
                        self, "Load Cascade",
                        "Loading a cascade will replace current steps. Continue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                missing_tools, missing_presets = _check_cascade_dependencies(data)
                if missing_tools or missing_presets:
                    if not _prompt_cascade_missing_deps(self, missing_tools, missing_presets):
                        return
                try:
                    dock._import_cascade_data(data)
                except ValueError as e:
                    self.statusBar().showMessage(f"Invalid cascade: {e}")
                    return
                name = data.get("name", Path(path).stem)
                missing = 0
                for step in data.get("steps", []):
                    if step.get("tool") and _resolve_app_relative(step["tool"]) is None:
                        missing += 1
                if missing:
                    self.statusBar().showMessage(
                        f"Loaded cascade: {name} — {missing} tool(s) not found"
                    )
                else:
                    self.statusBar().showMessage(f"Loaded cascade: {name}")
                return
            self._load_tool_path(path)
            return

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_picker(self) -> None:
        """Switch to the tool-picker view and reset form-specific menu items."""
        self._autosave_timer.stop()
        self.picker.scan()
        self.stack.setCurrentIndex(0)
        self.setWindowTitle("Scaffold")
        self.act_reload.setEnabled(False)
        self.act_back.setEnabled(False)
        self.preset_menu.setEnabled(False)
        self.act_history.setEnabled(False)
        if hasattr(self, "reset_btn"):
            self.reset_btn.setEnabled(False)
        self.picker.back_btn.setEnabled(self.data is not None)
        self.statusBar().showMessage("Ready")

    def _on_picker_back(self) -> None:
        """Return from the tool picker to the previously loaded tool form."""
        if self.data is not None:
            self.stack.setCurrentIndex(1)
            self.setWindowTitle(f"Scaffold \u2014 {self.data['tool']}")
            self.act_reload.setEnabled(True)
            self.act_back.setEnabled(True)
            self.preset_menu.setEnabled(True)
            self.act_history.setEnabled(True)
            self.reset_btn.setEnabled(True)

    def _record_recent_tool(self, path: str) -> None:
        """Record a tool path as recently used (MRU, capped at 5)."""
        raw = self.settings.value("picker/recent_tools", "")
        recent: list[str] = []
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    recent = [p for p in loaded if isinstance(p, str)]
            except (json.JSONDecodeError, TypeError):
                recent = []
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        del recent[5:]
        self.settings.setValue("picker/recent_tools", json.dumps(recent))

    def _load_tool_path(self, path: str) -> None:
        """Load, validate, and display the tool at path; show an error dialog on failure."""
        self._autosave_timer.stop()
        try:
            data = load_tool(path)
        except RuntimeError as e:
            self._show_load_error(str(e))
            return

        # Three-tier _format check: correct -> silent, missing -> warn, wrong -> reject
        fmt = data.get("_format")
        if fmt == "scaffold_cascade":
            QMessageBox.critical(
                self, "Wrong File Format",
                "This is a cascade file, not a tool schema. "
                "Use Cascade \u2192 Cascade List to open it.",
            )
            return
        if fmt is not None and fmt != "scaffold_schema":
            QMessageBox.critical(
                self, "Wrong File Format",
                f'This file has "_format": "{fmt}" \u2014 '
                f"it appears to be a preset, not a tool schema."
                if fmt == "scaffold_preset"
                else f'This file has "_format": "{fmt}" \u2014 '
                f"it is not a Scaffold tool schema.",
            )
            return
        if fmt is None:
            btn = QMessageBox.warning(
                self, "Missing Format Marker",
                "This file doesn't contain a format marker. "
                "It may not be a Scaffold tool schema.\n\nLoad anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        errors = validate_tool(data)
        if errors:
            self._show_load_error(
                f"Validation failed for {Path(path).name}:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
            return

        data = normalize_tool(data)
        self.data = data
        self.tool_path = path
        self._build_form_view()
        self.stack.setCurrentIndex(1)
        self._record_recent_tool(path)
        self.setWindowTitle(f"Scaffold \u2014 {data['tool']}")
        saved_timeout = int(self.settings.value(f"timeout/{data['tool']}", 0))
        self.timeout_spin.blockSignals(True)
        self.timeout_spin.setValue(saved_timeout)
        self.timeout_spin.blockSignals(False)
        self.act_reload.setEnabled(True)
        self.act_back.setEnabled(True)
        self.preset_menu.setEnabled(True)
        self.act_history.setEnabled(True)
        self.reset_btn.setEnabled(True)
        self.settings.setValue("session/last_tool", path)
        # Count fields and required fields for status message
        total_fields = len(data.get("arguments") or [])
        required_fields = sum(
            1 for a in (data.get("arguments") or []) if a.get("required")
        )
        if data.get("subcommands"):
            for sub in data["subcommands"]:
                total_fields += len(sub.get("arguments") or [])
                required_fields += sum(
                    1 for a in (sub.get("arguments") or []) if a.get("required")
                )
        self.statusBar().showMessage(
            f"Loaded {data['tool']} — {total_fields} fields ({required_fields} required)"
        )

        # Binary-in-PATH warning
        if not _binary_in_path(data["binary"]):
            self._set_warning_bar_text(data["binary"])
            self.warning_bar.setVisible(True)
        else:
            self.warning_bar.setVisible(False)

        self._default_form_snapshot = self.form.serialize_values()
        self._check_for_recovery()

    def _show_load_error(self, msg: str) -> None:
        """Display a warning dialog for tool-load failures."""
        QMessageBox.warning(self, "Load Error", msg)

    def _build_form_view(self):
        """Tear down old form widgets and build fresh ones for self.data."""
        self._copy_password_choice = None
        # Kill any running process
        self._elapsed_timer.stop()
        self._teardown_process()

        # Preserve output content during chain execution
        _preserved_output = None
        if self._chain_preserve_output and hasattr(self, 'output'):
            _preserved_output = self.output.document().toHtml()

        # Clear old contents (hide + detach synchronously to prevent
        # ghost widgets on Linux where deleteLater timing differs)
        layout = self.form_container_layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                w = child.widget()
                w.hide()
                w.setParent(None)
                w.deleteLater()

        # Warning banner (non-blocking, at top)
        self.warning_bar = QLabel("")
        self.warning_bar.setOpenExternalLinks(False)
        self.warning_bar.setTextFormat(Qt.TextFormat.RichText)
        self.warning_bar.linkActivated.connect(lambda _: self._on_custom_paths())
        self._style_warning_bar()
        self.warning_bar.setVisible(False)
        layout.addWidget(self.warning_bar)

        # Form inside a bordered frame
        self.form = ToolForm(self.data)
        self.form_frame = QFrame()
        self.form_frame.setFrameShape(QFrame.Shape.Box)
        frame_layout = QVBoxLayout(self.form_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.addWidget(self.form)
        self._style_form_frame()
        layout.addWidget(self.form_frame, 1)

        # -- Command Preview section --
        preview_section = QVBoxLayout()
        preview_section.setSpacing(5)
        preview_section.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QLabel("Command Preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._style_section_label(self.preview_label)
        self.preview_label.setStyleSheet(
            self.preview_label.styleSheet().replace("2px 0 1px 0;", "0px 0 0px 0;"))
        self.preview_label.setContentsMargins(0, 0, 0, 0)
        preview_section.addWidget(self.preview_label)

        preview_bar = QHBoxLayout()
        preview_bar.setContentsMargins(8, 0, 8, 0)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(_monospace_font())
        self.preview.setPlaceholderText("Command preview...")
        self.preview.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.preview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.preview.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        line_height = self.preview.fontMetrics().lineSpacing()
        scrollbar_height = QApplication.style().pixelMetric(QApplication.style().PixelMetric.PM_ScrollBarExtent)
        self.preview.setFixedHeight(line_height + scrollbar_height + 12)
        if _dark_mode:
            self.preview.setStyleSheet(
                f"QTextEdit {{ background-color: {DARK_COLORS['widget']};"
                f" color: {DARK_COLORS['text']}; }}"
            )
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._on_preview_context_menu)
        preview_bar.addWidget(self.preview, 1)

        btn_col = QVBoxLayout()
        btn_col.setContentsMargins(0, 0, 0, 0)
        self.reset_btn = QPushButton("Reset to Defaults")
        self.reset_btn.setEnabled(False)
        self.reset_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.reset_btn.clicked.connect(self._on_reset_defaults)
        btn_col.addWidget(self.reset_btn)
        btn_col.addSpacing(12)
        # Determine default copy format from platform, then override from settings
        _default_fmt = "cmd" if sys.platform == "win32" else "bash"
        self._copy_format = self.settings.value("copy_format", _default_fmt)
        if self._copy_format not in _COPY_FORMATS:
            self._copy_format = _default_fmt

        self.copy_btn = QToolButton()
        self.copy_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.copy_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.copy_btn.clicked.connect(self._copy_command)
        self._copy_format_menu = QMenu(self.copy_btn)
        self._copy_format_menu.addAction("Copy as Bash", lambda: self._set_copy_format("bash"))
        self._copy_format_menu.addAction("Copy as PowerShell", lambda: self._set_copy_format("powershell"))
        self._copy_format_menu.addAction("Copy as CMD", lambda: self._set_copy_format("cmd"))
        self._copy_format_menu.addSeparator()
        self._copy_format_menu.addAction("Copy (Generic)", lambda: self._set_copy_format("generic"))
        self.copy_btn.setMenu(self._copy_format_menu)
        self._update_copy_label()
        fm = self.copy_btn.fontMetrics()
        widest = max(
            fm.horizontalAdvance("Copy (PowerShell)"),
            fm.horizontalAdvance("Copy (Bash)"),
            fm.horizontalAdvance("Copy (CMD)"),
            fm.horizontalAdvance("Copy Command"),
        )
        self.copy_btn.setFixedWidth(widest + 44)
        self.copy_btn.setFixedHeight(26)
        btn_col.addWidget(self.copy_btn)
        preview_bar.addLayout(btn_col)

        preview_widget = QWidget()
        preview_widget.setLayout(preview_bar)
        preview_section.addWidget(preview_widget)
        # Status label
        self.status = QLabel("")
        self.status.setStyleSheet("padding: 0 8px 0 8px;")
        preview_section.addWidget(self.status)
        preview_section_widget = QWidget()
        preview_section_widget.setLayout(preview_section)
        layout.addWidget(preview_section_widget)

        # Action buttons bar
        action_widget = QWidget()
        action_bar = QHBoxLayout(action_widget)
        action_bar.setContentsMargins(8, 0, 8, 0)

        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._on_run_stop)
        self._style_run_btn()
        _run_font = self.run_btn.font()
        _run_font.setBold(True)
        self.run_btn.setFont(_run_font)
        self.run_btn.setMinimumWidth(QFontMetrics(_run_font).horizontalAdvance("Stopping...") + 40)
        self.run_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        action_bar.addWidget(self.run_btn)

        self.clear_btn = QPushButton("Clear Output")
        self.clear_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.clear_btn.clicked.connect(self._clear_output)
        action_bar.addWidget(self.clear_btn)

        self.copy_output_btn = QPushButton("Copy Output")
        self.copy_output_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.copy_output_btn.clicked.connect(self._copy_output)
        action_bar.addWidget(self.copy_output_btn)

        self.save_output_btn = QPushButton("Save Output")
        self.save_output_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.save_output_btn.clicked.connect(self._save_output)
        action_bar.addWidget(self.save_output_btn)

        _small_font = self.clear_btn.font()
        _small_font.setPointSize(8)
        self.clear_btn.setFont(_small_font)
        self.copy_output_btn.setFont(_small_font)
        self.save_output_btn.setFont(_small_font)
        for btn in (self.clear_btn, self.copy_output_btn, self.save_output_btn):
            fm = btn.fontMetrics()
            btn.setMinimumWidth(fm.horizontalAdvance(btn.text()) + 24)

        action_bar.addSpacerItem(QSpacerItem(16, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        timeout_label = QLabel("Timeout (s):")
        action_bar.addWidget(timeout_label)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 99999)
        self.timeout_spin.setToolTip("Auto-kill after N seconds (0 = no timeout)")
        self.timeout_spin.setFixedWidth(80)
        self.timeout_spin.valueChanged.connect(self._on_timeout_changed)
        action_bar.addWidget(self.timeout_spin)

        layout.addWidget(action_widget)

        # -- Output section --
        self.output_label = QLabel("Output")
        self.output_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._style_section_label(self.output_label)
        layout.addWidget(self.output_label)

        # Output search bar (Ctrl+Shift+F) — initially hidden
        self._output_search_bar = QLineEdit()
        self._output_search_bar.setPlaceholderText("Search output...  (Ctrl+Shift+F)")
        self._output_search_bar.textChanged.connect(self._on_output_search_changed)
        self._output_search_bar.installEventFilter(self)
        self._output_search_matches: list[tuple[int, int]] = []  # (start, length)
        self._output_search_index = -1
        self._output_search_count_label = QLabel("")
        self._output_search_count_label.setStyleSheet("margin-left: 4px;")
        output_search_row = QHBoxLayout()
        output_search_row.setContentsMargins(0, 0, 0, 0)
        output_search_row.addWidget(self._output_search_bar, 1)
        output_search_row.addWidget(self._output_search_count_label)
        self._output_search_widget = QWidget()
        self._output_search_widget.setLayout(output_search_row)
        self._output_search_widget.setVisible(False)
        layout.addWidget(self._output_search_widget)

        # Output panel (create first so DragHandle can reference it)
        saved_height = int(self.settings.value("output/height", OUTPUT_DEFAULT_HEIGHT))
        saved_height = max(OUTPUT_MIN_HEIGHT, saved_height)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(_monospace_font())
        self.output.setFixedHeight(saved_height)
        self.output.setMaximumBlockCount(OUTPUT_MAX_BLOCKS)

        # Drag handle to resize output panel
        self.output_handle = DragHandle(self.output, self.settings)
        layout.addWidget(self.output_handle)
        if _dark_mode:
            self.output.setStyleSheet(
                f"QPlainTextEdit {{ background-color: {DARK_COLORS['output_bg']};"
                f" color: {DARK_COLORS['output_text']}; }}"
            )
        else:
            self.output.setStyleSheet(
                f"QPlainTextEdit {{ background-color: {OUTPUT_BG}; color: {OUTPUT_FG}; }}"
            )
        layout.addWidget(self.output)

        # Restore preserved output from previous chain step
        if _preserved_output is not None:
            self.output.document().setHtml(_preserved_output)

        # Output batching buffer — collect readyRead data and flush on a timer
        self._output_buffer: list[tuple[str, str]] = []
        self._output_truncated = False
        # Per-run stream buffers for cascade capture extraction
        self._last_run_stdout: str = ""
        self._last_run_stderr: str = ""

        # Connect live preview
        self.form.command_changed.connect(self._update_preview)
        self.form.command_changed.connect(self._autosave_on_change)
        self._update_preview()

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------

    def _on_load_file(self) -> None:
        """Open a file-picker dialog and load the chosen JSON tool schema."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Tool JSON", str(_tools_dir()), "JSON Files (*.json)"
        )
        if path:
            self._load_tool_path(path)

    def _on_reload(self) -> None:
        """Reload the current tool schema from disk."""
        if self.tool_path:
            self._load_tool_path(self.tool_path)

    def _on_back(self) -> None:
        """Return to the tool picker, stopping any running process first."""
        # Kill any running process before going back
        self._teardown_process()
        self._show_picker()

    def _on_custom_paths(self) -> None:
        """Open the custom PATH directories dialog and refresh on changes."""
        old = _get_custom_paths()
        dlg = CustomPathDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and _get_custom_paths() != old:
            # Re-check binary availability for current tool
            if self.data and hasattr(self, "warning_bar"):
                binary = self.data.get("binary", "")
                if binary and not _binary_in_path(binary):
                    self._set_warning_bar_text(binary)
                    self.warning_bar.setVisible(True)
                else:
                    self.warning_bar.setVisible(False)
            # Rescan tool picker if visible
            if self.stack.currentIndex() == 0:
                self.picker.scan()

    def _on_reset_pw_copy_prompt(self) -> None:
        """Clear the remembered copy-with-passwords choice for the session."""
        self._copy_password_choice = None
        self.statusBar().showMessage("Password copy prompt reset")

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _on_save_preset(self) -> None:
        """Prompt for a name and save the current form state as a preset JSON file."""
        if not self.data:
            return
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        # Sanitize filename (match cascade save pattern)
        safe_name = _sanitize_filename_component(name)
        if not safe_name:
            QMessageBox.warning(self, "Invalid Name", "The name is not valid after sanitizing.")
            return

        preset_dir = _presets_dir(self.data["tool"])
        preset_path = preset_dir / f"{safe_name}.json"

        # Pre-fill description if overwriting
        existing_desc = ""
        if preset_path.exists():
            answer = QMessageBox.question(
                self, "Overwrite Preset",
                f"Overwrite existing preset '{safe_name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                existing_data = _read_json_file(preset_path)
                existing_desc = existing_data.get("_description", "")
            except (json.JSONDecodeError, OSError):
                pass

        description, ok2 = QInputDialog.getText(
            self, "Save Preset", "Description (optional):", text=existing_desc,
        )
        if not ok2:
            return
        description = description.strip()

        preset = self.form.serialize_values()
        preset["_description"] = description
        try:
            _atomic_write_json(preset_path, preset)
        except OSError as e:
            self.statusBar().showMessage(f"Error saving preset: {e}")
            return
        self.statusBar().showMessage(f"Preset saved: {safe_name}")
        self._clear_recovery_file()

    def _on_load_preset(self) -> None:
        """Show a preset picker and restore the selected preset to the form."""
        if not self.data:
            return
        preset_dir = _presets_dir(self.data["tool"])
        presets = sorted(preset_dir.glob("*.json"))
        if not presets:
            self.statusBar().showMessage("No saved presets for this tool")
            return

        picker = PresetPicker(self.data["tool"], preset_dir, mode="load", parent=self)
        if not picker.exec() or not picker.selected_path:
            return

        self._apply_preset_from_path(picker.selected_path)

    def _apply_preset_from_path(self, path: str) -> None:
        """Validate and apply the preset file at `path` to the open tool form.

        Caller must ensure self.data is not None — both call sites
        (_on_load_preset, dropEvent) guard that before invoking.
        """
        assert self.data is not None
        preset_path = Path(path)
        name = preset_path.stem

        # Size guard — preset size cap (MAX_PRESET_SIZE)
        try:
            size = preset_path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_PRESET_SIZE:
            QMessageBox.warning(
                self, "Preset Too Large",
                f"Preset file too large ({size:,} bytes, limit {MAX_PRESET_SIZE:,}).",
            )
            return

        try:
            preset = _read_json_file(preset_path)
        except (json.JSONDecodeError, OSError) as e:
            self.statusBar().showMessage(f"Error loading preset: {e}")
            return

        # Three-tier _format check: correct -> silent, missing -> prompt, wrong -> reject
        fmt = preset.get("_format")
        if fmt is not None and fmt != "scaffold_preset":
            QMessageBox.critical(
                self, "Wrong File Format",
                f'This file has "_format": "{fmt}" \u2014 '
                f"it appears to be a tool schema, not a preset."
                if fmt == "scaffold_schema"
                else f'This file has "_format": "{fmt}" \u2014 '
                f"it is not a Scaffold preset.",
            )
            return
        if fmt is None:
            btn = QMessageBox.warning(
                self, "Missing Format Marker",
                "This preset doesn't contain a format marker. "
                "It may not be a Scaffold preset.\n\nLoad anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        # Validate preset structure + per-key types against the live tool's
        # schema. report_unknown_keys=False so old presets with renamed flags
        # still load with the schema_hash status-bar warning shown below;
        # type errors are hard rejects (silent-bug catch from v2.11.3).
        verrors = validate_preset(preset, tool_data=self.data, report_unknown_keys=False)
        if verrors:
            QMessageBox.warning(
                self, "Invalid Preset",
                f"Preset '{name}' failed validation:\n"
                + "\n".join(f"  - {e}" for e in verrors),
            )
            return

        # Tool identity check — fires before the schema-hash warning so the
        # user sees the more specific message first. Absent _tool is treated
        # as legacy/unknown and silently allowed.
        saved_tool = preset.get("_tool")
        current_tool = self.data["tool"]
        if saved_tool is not None and saved_tool != current_tool:
            btn = QMessageBox.warning(
                self, "Tool Mismatch",
                f"This preset was made for {saved_tool!r} but you're loading "
                f"it into {current_tool!r}. Flags may not apply correctly.\n\n"
                f"Load anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        had_pw = self.form.apply_values(preset)
        self._default_form_snapshot = self.form.serialize_values()

        # Check schema hash for version mismatch
        saved_hash = preset.get("_schema_hash")
        if saved_hash is not None and saved_hash != schema_hash(self.data):
            self.statusBar().showMessage(
                f"Preset loaded: {name} \u2014 Note: This preset was saved with a different "
                "schema version. Some fields may not have loaded."
            )
        elif had_pw:
            self.statusBar().showMessage(
                f"Preset loaded: {name} \u2014 password fields must be re-entered."
            )
        else:
            self.statusBar().showMessage(f"Preset loaded: {name}")

    def _on_edit_preset(self) -> None:
        """Open the preset picker in edit mode for managing descriptions and deleting."""
        if not self.data:
            return
        preset_dir = _presets_dir(self.data["tool"])
        presets = sorted(preset_dir.glob("*.json"))
        if not presets:
            self.statusBar().showMessage("No presets to edit")
            return
        picker = PresetPicker(self.data["tool"], preset_dir, mode="edit", parent=self)
        picker.exec()
        if getattr(picker, "_deleted_last", False):
            self.statusBar().showMessage("No presets remaining")

    def _on_import_preset(self) -> None:
        """Import a preset JSON file into the current tool's preset directory."""
        if not self.data:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Preset", "", "JSON files (*.json)",
        )
        if not path:
            return
        src = Path(path)

        # Size guard — preset size cap (MAX_PRESET_SIZE)
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        if size > MAX_PRESET_SIZE:
            QMessageBox.warning(
                self, "Import Failed",
                f"Preset file too large ({size:,} bytes, limit {MAX_PRESET_SIZE:,}).",
            )
            return

        try:
            data = _read_json_file(src)
        except (json.JSONDecodeError, OSError) as e:
            self.statusBar().showMessage(f"Import failed: invalid JSON — {e}")
            return
        if not isinstance(data, dict):
            self.statusBar().showMessage("Import failed: file is not a JSON object")
            return

        # Three-tier _format check: correct -> silent, wrong -> reject, missing -> warn
        fmt = data.get("_format")
        if fmt == "scaffold_cascade":
            QMessageBox.critical(
                self, "Wrong File Format",
                "This is a cascade file, not a preset. "
                "Use Cascade \u2192 Import Cascade to open it.",
            )
            return
        if fmt is not None and fmt != "scaffold_preset":
            QMessageBox.critical(
                self, "Wrong File Format",
                f'This file has "_format": "{fmt}" \u2014 '
                f"it appears to be a tool schema, not a preset."
                if fmt == "scaffold_schema"
                else f'This file has "_format": "{fmt}" \u2014 '
                f"it is not a Scaffold preset.",
            )
            return
        if fmt is None:
            btn = QMessageBox.warning(
                self, "Missing Format Marker",
                "This file doesn't contain a format marker. "
                "It may not be a Scaffold preset.\n\nImport anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        # Validate preset structure before importing
        verrors = validate_preset(data)
        if verrors:
            QMessageBox.warning(
                self, "Import Failed",
                "Preset failed validation:\n"
                + "\n".join(f"  - {e}" for e in verrors),
            )
            return

        # Tool identity check — surface wrong-tool imports before we copy the
        # file into the preset dir. Absent _tool is legacy and silently allowed.
        saved_tool = data.get("_tool")
        current_tool = self.data["tool"]
        if saved_tool is not None and saved_tool != current_tool:
            btn = QMessageBox.warning(
                self, "Tool Mismatch",
                f"This preset was made for {saved_tool!r} but you're importing "
                f"it into {current_tool!r}. Flags may not apply correctly.\n\n"
                f"Import anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return

        name = src.stem
        preset_dir = _presets_dir(self.data["tool"])
        dest = preset_dir / f"{name}.json"

        if dest.exists():
            answer = QMessageBox.question(
                self, "Overwrite Preset",
                f"Overwrite existing preset '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            _atomic_write_json(dest, data)
        except OSError as e:
            self.statusBar().showMessage(f"Import failed: {e}")
            return
        self.statusBar().showMessage(f"Preset imported: {name}")

    def _on_export_preset(self) -> None:
        """Export an existing preset to a user-chosen location."""
        if not self.data:
            return
        preset_dir = _presets_dir(self.data["tool"])
        presets = sorted(preset_dir.glob("*.json"))
        if not presets:
            self.statusBar().showMessage("No presets to export")
            return

        names = [p.stem for p in presets]
        name, ok = QInputDialog.getItem(
            self, "Export Preset", "Select preset:", names, 0, False,
        )
        if not ok:
            return

        preset_path = preset_dir / f"{name}.json"
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export Preset", f"{name}.json", "JSON files (*.json)",
        )
        if not dest:
            return

        try:
            content = preset_path.read_text(encoding="utf-8")
            _atomic_write_json(Path(dest), json.loads(content))
        except (OSError, json.JSONDecodeError) as e:
            self.statusBar().showMessage(f"Export failed: {e}")
            return
        self.statusBar().showMessage(f"Preset exported: {name}")

    def _on_reset_defaults(self) -> None:
        """Reset all form fields to their schema-defined defaults."""
        if self.data:
            self.form.reset_to_defaults()
            self._default_form_snapshot = self.form.serialize_values()
            self._clear_recovery_file()
            QTimer.singleShot(0, lambda: self._show_status(
                "Reset to defaults",
                DARK_COLORS["success"] if _dark_mode else "green",
            ))

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _update_preview(self) -> None:
        """Rebuild the command preview and toggle Run button availability."""
        missing = self.form.validate_required()
        cmd, display = self.form.build_command()
        extra_count = len(self.form.get_extra_flags())
        if self.form.is_elevation_checked():
            elev_cmd, _ = get_elevation_command(cmd)
            extra_count = len(self.form.get_extra_flags())
            cmd = elev_cmd
            display = _format_display(elev_cmd)
        sub = self.form.sub_combo.currentData() if self.form.sub_combo else None
        masked_cmd = self.form._mask_passwords_for_display(cmd)
        html = _colored_preview_html(masked_cmd, extra_count, subcommand=sub)
        self.preview.setHtml(html)
        process_running = (
            self.process is not None
            and self.process.state() != QProcess.ProcessState.NotRunning
        )
        if missing:
            names = []
            for key in missing:
                field = self.form.fields.get(key)
                if field:
                    names.append(field["arg"]["name"])
            color = DARK_COLORS["error"] if _dark_mode else "red"
            self.status.setText(f"Required: {', '.join(names)}")
            self.status.setStyleSheet(f"color: {color}; padding: 0 8px 4px 8px;")
            # Do NOT start _status_timer — this message must persist until fields are filled
            self._status_timer.stop()
            # Keep Stop button clickable while a process is running
            self.run_btn.setEnabled(process_running)
        elif not self.form.extra_flags_valid():
            self.run_btn.setEnabled(process_running)
        else:
            self.status.setText("")
            self.status.setStyleSheet("padding: 0 8px 4px 8px;")
            self.run_btn.setEnabled(True)

    def _prepare_copy_cmd(self, cmd: list[str]) -> tuple[list[str] | None, str]:
        """Apply password masking policy to a command before copying.

        Returns a (cmd, pw_state) tuple where pw_state is one of
        "none" (no password fields with values), "real" (real passwords
        copied), or "masked" (passwords replaced with ********).
        cmd is None if the user dismissed the dialog via window-close
        (treated as masked, choice not stored).
        """
        # Check if any password fields have non-None values
        has_pw = False
        sub = self.form.get_current_subcommand()
        scopes = {self.form.GLOBAL}
        if sub:
            scopes.add(sub)
        for key, field in self.form.fields.items():
            scope, flag = key
            if scope not in scopes:
                continue
            if field["arg"]["type"] != "password":
                continue
            if self.form.get_field_value(key) is not None:
                has_pw = True
                break

        if not has_pw:
            return cmd, "none"

        if self._copy_password_choice is None:
            answer = QMessageBox.question(
                self, "Copy with Passwords?",
                "This command contains password fields with values.\n"
                "Copy the real passwords to the clipboard, or replace them "
                "with ********?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._copy_password_choice = True
            elif answer == QMessageBox.StandardButton.No:
                self._copy_password_choice = False
            else:
                # Dialog dismissed (window close) — mask but don't store
                return self.form._mask_passwords_for_display(cmd), "masked"

        if self._copy_password_choice:
            return cmd, "real"
        return self.form._mask_passwords_for_display(cmd), "masked"

    def _copy_command(self) -> None:
        """Copy the current command using the selected shell format to the clipboard."""
        cmd, _ = self.form.build_command()
        if self.form.is_elevation_checked():
            cmd, _ = get_elevation_command(cmd)
        cmd, pw_state = self._prepare_copy_cmd(cmd)
        if cmd is None:
            return
        formatter, _ = _COPY_FORMATS.get(self._copy_format, (_format_display, "Command"))
        text = formatter(cmd)
        QApplication.clipboard().setText(text)
        color = DARK_COLORS["success"] if _dark_mode else "green"
        self._show_status("Copied to clipboard." + _pw_copy_suffix(pw_state), color)

    def _set_copy_format(self, fmt: str) -> None:
        """Set the copy format, update the button label, persist, and immediately copy."""
        self._copy_format = fmt
        self.settings.setValue("copy_format", fmt)
        self._update_copy_label()
        self._copy_command()

    def _update_copy_label(self) -> None:
        """Update the copy button label to reflect the current format."""
        if self._copy_format == "generic":
            self.copy_btn.setText("Copy Command")
        else:
            _, label = _COPY_FORMATS.get(self._copy_format, (_format_display, "Command"))
            self.copy_btn.setText(f"Copy ({label})")

    def _on_preview_context_menu(self, pos) -> None:
        """Show context menu with shell-specific copy options."""
        if self.form.data is None:
            return
        menu = QMenu(self.preview)
        menu.addAction("Copy as Bash", self._copy_as_bash)
        menu.addAction("Copy as PowerShell", self._copy_as_powershell)
        menu.addAction("Copy as CMD", self._copy_as_cmd)
        menu.addSeparator()
        menu.addAction("Copy", self._copy_command)
        menu.exec(self.preview.mapToGlobal(pos))

    def _copy_as_shell(self, formatter, label: str) -> None:
        """Helper: copy command formatted for a specific shell."""
        cmd, _ = self.form.build_command()
        if self.form.is_elevation_checked():
            cmd, _ = get_elevation_command(cmd)
        cmd, pw_state = self._prepare_copy_cmd(cmd)
        if cmd is None:
            return
        text = formatter(cmd)
        QApplication.clipboard().setText(text)
        color = DARK_COLORS["success"] if _dark_mode else "green"
        self._show_status(f"Copied as {label}." + _pw_copy_suffix(pw_state), color)

    def _copy_as_bash(self) -> None:
        self._copy_as_shell(_format_bash, "Bash")

    def _copy_as_powershell(self) -> None:
        self._copy_as_shell(_format_powershell, "PowerShell")

    def _copy_as_cmd(self) -> None:
        self._copy_as_shell(_format_cmd, "CMD")

    def _copy_output(self) -> None:
        """Copy the output panel's plain text to the clipboard."""
        text = self.output.toPlainText()
        if not text.strip():
            self._show_status("No output to copy")
            return
        QApplication.clipboard().setText(text)
        color = DARK_COLORS["success"] if _dark_mode else "green"
        self._show_status("Output copied to clipboard.", color)

    def _show_status(self, text: str, color: str | None = None) -> None:
        """Display a colored message in the status label below the command preview."""
        if color is None:
            color = DARK_COLORS["error"] if _dark_mode else "red"
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; padding: 0 8px 4px 8px;")
        self._status_timer.start(3000)

    # ------------------------------------------------------------------
    # Process execution
    # ------------------------------------------------------------------

    def _on_timeout_changed(self, value: int) -> None:
        """Persist the timeout value per tool in QSettings."""
        if self.data:
            self.settings.setValue(f"timeout/{self.data['tool']}", value)

    def _stop_process(self) -> None:
        """Stop the running process. Uses SIGTERM first, SIGKILL as fallback."""
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._killed = True
        self._timeout_timer.stop()
        self.process.terminate()  # SIGTERM — pkexec can forward this
        self._force_kill_timer.start()

    def _on_force_kill(self) -> None:
        """Escalate to SIGKILL if the process did not exit after SIGTERM."""
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        if not self._elevated_run and sys.platform != "win32":
            pid = self.process.processId()
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        self.process.kill()

    def _teardown_process(self) -> None:
        """Synchronous full teardown of the current QProcess.

        Used by _build_form_view, _on_back, and closeEvent — call sites that
        must guarantee the process is dead before proceeding.  Unlike
        _stop_process (which relies on the force-kill timer for escalation),
        this method disconnects all signals, terminates, waits, and kills
        inline so no orphan QProcess can later corrupt UI state.
        """
        if self.process is None:
            return
        # Stop the force-kill timer — we handle escalation ourselves.
        self._force_kill_timer.stop()
        # Disconnect all signals so the orphan can never call back.
        for sig in (
            self.process.finished,
            self.process.errorOccurred,
            self.process.readyReadStandardOutput,
            self.process.readyReadStandardError,
        ):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        # Terminate → wait → kill → wait.
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)
        # Remove from Qt parent-child tree so it cannot linger as a child
        # of MainWindow, then schedule C++ destruction.
        self.process.setParent(None)
        self.process.deleteLater()
        # Reset state (no UI updates — callers handle that).
        self.process = None
        self._killed = False
        self._error_reported = False
        self._timed_out = False
        self._run_start_time = None

    def _on_timeout_fired(self) -> None:
        """Kill the running process because the timeout expired."""
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            timeout_val = self.timeout_spin.value()
            self._timed_out = True
            self._append_output(
                f"\n--- Process timed out after {timeout_val} seconds ---\n",
                COLOR_WARN,
            )
            self.run_btn.setText("Stopping...")
            self.run_btn.setEnabled(False)
            self._style_run_btn()
            QApplication.processEvents()  # Force repaint before kill
            self._stop_process()

    def _on_run_stop(self) -> None:
        """Run the command if idle, or stop the running process if one is active."""
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self.run_btn.setText("Stopping...")
            self.run_btn.setEnabled(False)
            self._style_run_btn()
            QApplication.processEvents()  # Force repaint before kill
            self._stop_process()
            return

        # Validate required fields
        missing = self.form.validate_required()
        if missing:
            return

        # Block execution when extra flags have unmatched quotes
        if not self.form.extra_flags_valid():
            self._show_status(
                "Fix extra flags before running \u2014 unmatched quotes detected",
                DARK_COLORS["error"] if _dark_mode else "red",
            )
            return

        cmd, display = self.form.build_command()
        if not cmd:
            return

        # Handle elevation
        self._elevated_run = False
        if self.form.is_elevation_checked():
            elev_cmd, error = get_elevation_command(cmd)
            if error:
                QMessageBox.warning(self, "Elevation Not Available", error)
                return
            cmd = elev_cmd
            display = _format_display(cmd)
            self._elevated_run = True

        program = cmd[0]
        arguments = cmd[1:]

        # Show what we're running (mask passwords in display output)
        masked_display = _format_display(self.form._mask_passwords_for_display(cmd))
        self._append_output(f"$ {masked_display}\n", COLOR_CMD)
        self._update_output_search_visibility()

        # Capture history data at run start (before user can change the form).
        # Set BEFORE signal connections so errorOccurred can't fire before these exist.
        self._history_display = _format_display(self.form._mask_passwords_for_display(cmd))
        self._history_preset = self.form.serialize_values()
        self._history_timestamp = time.time()

        self.process = QProcess(self)
        # Inject custom PATH directories into process environment
        custom_paths = _get_custom_paths()
        if custom_paths:
            env = QProcessEnvironment.systemEnvironment()
            current_path = env.value("PATH", "")
            env.insert("PATH", os.pathsep.join(custom_paths) + os.pathsep + current_path)
            self.process.setProcessEnvironment(env)
        self.process.setProgram(program)
        self.process.setArguments(arguments)
        self.process.readyReadStandardOutput.connect(self._on_stdout_ready)
        self.process.readyReadStandardError.connect(self._on_stderr_ready)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

        self._killed = False
        self._error_reported = False
        self._timed_out = False
        self._last_run_stdout = ""
        self._last_run_stderr = ""
        self._run_start_time = time.monotonic()
        self.run_btn.setText("Stop")
        self._style_run_btn()
        self._progress_label.setText("Running... (0s)")
        self._elapsed_timer.start()
        timeout_secs = self.timeout_spin.value()
        if timeout_secs > 0:
            self._timeout_timer.start(timeout_secs * 1000)
        self.process.start()

    def _append_to_buffer(self, text: str, color: str) -> None:
        """Append to _output_buffer with soft byte-cap enforcement."""
        self._output_buffer.append((text, color))
        total = sum(len(t) for t, _ in self._output_buffer)
        if total > OUTPUT_BUFFER_MAX_BYTES:
            if not self._output_truncated:
                self._output_buffer.insert(
                    0,
                    ("[output truncated — buffer cap reached]\n", COLOR_WARN),
                )
                self._output_truncated = True
            # Drop oldest entries (after the sentinel) until under cap
            while (sum(len(t) for t, _ in self._output_buffer) > OUTPUT_BUFFER_MAX_BYTES
                   and len(self._output_buffer) > 1):
                self._output_buffer.pop(1)

    def _on_stdout_ready(self) -> None:
        """Buffer available stdout data for the next flush cycle."""
        if not self.process:
            return
        data = self.process.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        self._last_run_stdout += text
        self._append_to_buffer(text, OUTPUT_FG)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _on_stderr_ready(self) -> None:
        """Buffer available stderr data for the next flush cycle."""
        if not self.process:
            return
        data = self.process.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        self._last_run_stderr += text
        self._append_to_buffer(text, COLOR_WARN)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_output(self) -> None:
        """Flush buffered output to the panel in a single batch update."""
        self._flush_timer.stop()
        if not self._output_buffer:
            return
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        for text, color in self._output_buffer:
            text = _ANSI_RE.sub('', text)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            cursor.insertText(text, fmt)
        self._output_buffer.clear()
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()
        self._update_output_search_visibility()

    def _update_elapsed(self) -> None:
        """Tick the status bar with elapsed seconds while a process is running."""
        if self._run_start_time is not None:
            elapsed = int(time.monotonic() - self._run_start_time)
            self._progress_label.setText(f"Running... ({elapsed}s)")

    def _on_finished(self, exit_code: int, exit_status) -> None:
        """Handle process termination: print exit status and re-enable the Run button."""
        self._elapsed_timer.stop()
        self._progress_label.setText("")
        self._timeout_timer.stop()
        self._force_kill_timer.stop()
        self._flush_output()
        if self._error_reported:
            # _on_error already handled UI and recorded history; just
            # clear process and return — no double-record needed.
            self.process = None
            return
        elapsed_str = ""
        if self._run_start_time is not None:
            elapsed = time.monotonic() - self._run_start_time
            elapsed_str = f" ({elapsed:.1f}s)"
            self._run_start_time = None
        crashed = exit_status == QProcess.ExitStatus.CrashExit
        if self._timed_out:
            self.statusBar().showMessage(f"Timed out{elapsed_str}")
        elif self._killed:
            self._append_output("\n--- Process stopped ---\n", COLOR_WARN)
            self.statusBar().showMessage(f"Process stopped{elapsed_str}")
        elif crashed and not self._killed and not self._timed_out:
            self._append_output("\n--- Process crashed ---\n", COLOR_ERR)
            self.statusBar().showMessage(f"Crashed{elapsed_str}")
        elif self._elevated_run and exit_code == 126:
            self._append_output(
                "\n--- Elevation failed: helper could not execute. ---\n", COLOR_ERR
            )
            self.statusBar().showMessage("Elevation failed")
        elif self._elevated_run and exit_code == 127:
            self._append_output(
                "\n--- Elevation cancelled or unavailable ---\n", COLOR_WARN
            )
            self.statusBar().showMessage("Elevation cancelled or unavailable")
        elif exit_code == 0:
            self._append_output(f"\n--- Process exited with code {exit_code} ---\n", COLOR_OK)
            self.statusBar().showMessage(f"Exit 0{elapsed_str}")
        else:
            self._append_output(f"\n--- Process exited with code {exit_code} ---\n", COLOR_ERR)
            self.statusBar().showMessage(f"Exit {exit_code}{elapsed_str}")
        self._record_history_entry(exit_code, crashed=crashed)
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run")
        self._style_run_btn()
        self._update_preview()
        self.process = None

    def _on_error(self, error) -> None:
        """Handle QProcess errors, printing a descriptive message to the output panel."""
        self._progress_label.setText("")
        if error == QProcess.ProcessError.Crashed and self._killed:
            return  # Handled by _on_finished
        self._error_reported = True
        error_messages = {
            QProcess.ProcessError.FailedToStart: (
                f"Error: '{self.data['binary']}' not found. "
                "Is it installed and in your PATH?"
                if not self._elevated_run else
                f"Error: Failed to start elevated command. "
                f"Check that '{self.data['binary']}' is installed and in your PATH."
            ),
            QProcess.ProcessError.Crashed: "Process crashed unexpectedly.",
            QProcess.ProcessError.Timedout: "Process timed out.",
            QProcess.ProcessError.WriteError: "Write error communicating with process.",
            QProcess.ProcessError.ReadError: "Read error communicating with process.",
        }
        msg = error_messages.get(error, f"Unknown process error ({error}).")
        self._append_output(f"\n--- {msg} ---\n", COLOR_ERR)
        self.statusBar().showMessage(msg)
        # Record history for the errored run
        _error_tokens = {
            QProcess.ProcessError.FailedToStart: "failed_to_start",
            QProcess.ProcessError.Crashed: "crashed",
            QProcess.ProcessError.Timedout: "timed_out",
            QProcess.ProcessError.WriteError: "write_error",
            QProcess.ProcessError.ReadError: "read_error",
        }
        self._record_history_entry(-1, error=_error_tokens.get(error, "unknown"))
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run")
        self._style_run_btn()
        self._update_preview()
        # Clean up timers and process — needed for FailedToStart where
        # _on_finished() is never called.  Harmless double-set for Crashed.
        self._timeout_timer.stop()
        self._flush_output()
        self._elapsed_timer.stop()
        self._force_kill_timer.stop()
        self._run_start_time = None
        self.process = None

    # ------------------------------------------------------------------
    # Command history
    # ------------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        """Load command history for the current tool from QSettings."""
        if self.data is None:
            return []
        raw = self.settings.value(f"history/{self.data['tool']}", "[]")
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(entries, list):
                return []
            return [e for e in entries if isinstance(e, dict)]
        except (json.JSONDecodeError, TypeError):
            return []

    def _record_history_entry(self, exit_code: int, *, crashed: bool = False, error: str | None = None) -> None:
        """Record a completed command execution in the history."""
        if self.data is None:
            return
        if not (hasattr(self, "_history_display")
                and hasattr(self, "_history_preset")
                and hasattr(self, "_history_timestamp")):
            return
        entry = {
            "display": self._history_display,
            "exit_code": exit_code,
            "timestamp": self._history_timestamp,
            "preset_data": self._history_preset,
            "crashed": crashed,
            "error": error,
            "notes": "",
        }
        history = self._load_history()
        entry = _enforce_history_entry_size(entry)
        history.insert(0, entry)
        history = history[:HISTORY_MAX_ENTRIES]
        history = _enforce_history_total_size(history)
        self.settings.setValue(
            f"history/{self.data['tool']}", json.dumps(history)
        )

    def _clear_history(self) -> None:
        """Remove all command history for the current tool."""
        if self.data is None:
            return
        self.settings.remove(f"history/{self.data['tool']}")

    def _update_history_notes(self, timestamp, notes: str) -> bool:
        """Update the notes field of the history entry matching *timestamp*.

        Returns True if an entry was found and updated, False otherwise.
        Persists the change to QSettings via the same key/format used by
        ``_record_history_entry``. The match is by exact timestamp value
        (a per-run unique identifier within a tool's history).
        """
        if self.data is None:
            return False
        history = self._load_history()
        for entry in history:
            if entry.get("timestamp") == timestamp:
                entry["notes"] = notes
                self.settings.setValue(
                    f"history/{self.data['tool']}", json.dumps(history)
                )
                return True
        return False

    # ------------------------------------------------------------------
    # Cascade history (storage layer)
    # ------------------------------------------------------------------

    def _load_cascade_history(self) -> list[dict]:
        """Load cascade run history from QSettings."""
        ver = self.settings.value("cascade_history/_version", None)
        try:
            ver_int = int(ver) if ver is not None else None
        except (ValueError, TypeError):
            return []
        if ver_int != CASCADE_HISTORY_VERSION:
            return []
        raw = self.settings.value("cascade_history/entries", "[]")
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(entries, list):
                return []
            return entries
        except (json.JSONDecodeError, TypeError):
            return []

    def _save_cascade_history(self, entries: list[dict]) -> None:
        """Write cascade run history to QSettings."""
        self.settings.setValue("cascade_history/_version", CASCADE_HISTORY_VERSION)
        self.settings.setValue("cascade_history/entries", json.dumps(entries))

    def _append_cascade_run(self, entry: dict) -> None:
        """Append a cascade run entry, enforcing the FIFO cap."""
        entries = self._load_cascade_history()
        entry = _enforce_history_entry_size(entry)
        entries.insert(0, entry)
        entries = entries[:CASCADE_HISTORY_MAX_ENTRIES]
        entries = _enforce_history_total_size(entries)
        self._save_cascade_history(entries)

    def _update_cascade_run(self, run_id: str, updates: dict) -> None:
        """Update fields on an existing cascade run entry by id."""
        entries = self._load_cascade_history()
        found = False
        for entry in entries:
            if entry.get("id") == run_id:
                entry.update(updates)
                found = True
                break
        if found:
            self._save_cascade_history(entries)

    def _clear_cascade_history(self) -> None:
        """Remove all cascade run history."""
        self.settings.remove("cascade_history/entries")
        self.settings.remove("cascade_history/_version")

    def _recover_crashed_cascade_runs(self) -> None:
        """Transition any 'running' cascade entries to 'crashed' status."""
        entries = self._load_cascade_history()
        changed = False
        for entry in entries:
            if entry.get("status") == "running":
                entry["status"] = "crashed"
                entry["finished_at"] = time.time()
                changed = True
        if changed:
            self._save_cascade_history(entries)

    def _on_show_history(self) -> None:
        """Show the command history dialog for the current tool."""
        if self.data is None:
            return
        history = self._load_history()
        if not history:
            self._show_status("No command history for this tool")
            return
        dlg = HistoryDialog(self.data["tool"], history, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_entry is not None:
            had_pw = self.form.apply_values(dlg.selected_entry["preset_data"])
            self._default_form_snapshot = self.form.serialize_values()
            if had_pw:
                self._show_status(
                    "Form restored — password fields must be re-entered.",
                    DARK_COLORS["success"] if _dark_mode else "green",
                )
            else:
                self._show_status("Form restored from history")

    def _on_show_cascade_history(self) -> None:
        """Show cascade history dialog and handle restore actions."""
        history = self._load_cascade_history()
        if not history:
            self._show_status("No cascade history")
            return
        dlg = CascadeHistoryDialog(history, parent=self)
        result_code = dlg.exec()
        # Persist any in-dialog deletes / clears
        self._save_cascade_history(dlg.history)
        if result_code != QDialog.DialogCode.Accepted or dlg.restore_result is None:
            return
        result = dlg.restore_result
        if result["action"] == "step":
            step = result["step"]
            tool_path = step.get("tool_path")
            if not tool_path or not Path(tool_path).exists():
                return  # dialog already warned
            self._load_tool_path(tool_path)
            preset_path = step.get("preset_path")
            if preset_path and Path(preset_path).exists():
                try:
                    preset_data = _read_json_file(preset_path)
                    had_pw = self.form.apply_values(preset_data)
                    self._default_form_snapshot = self.form.serialize_values()
                    msg = "Form restored from cascade step"
                    if had_pw:
                        msg += " \u2014 password fields must be re-entered"
                    self._show_status(msg)
                except (OSError, json.JSONDecodeError):
                    self._show_status("Preset file could not be loaded")
            else:
                self._show_status("Tool loaded from cascade step")
        elif result["action"] == "cascade":
            config = result["config"]
            self.cascade_dock._restore_cascade_config(config)
            # Ensure cascade sidebar is visible
            if not self.cascade_dock.isVisible():
                self.cascade_dock.setVisible(True)
                self.act_cascade_toggle.setChecked(True)
                self.setMinimumWidth(MIN_WINDOW_WIDTH + 320)
            self._show_status("Cascade restored from history")

    def _append_output(self, text: str, color: str) -> None:
        """Append text to the output panel in the given hex color string."""
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text, fmt)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _clear_output(self) -> None:
        """Clear all text from the output panel."""
        self.output.clear()
        self._output_buffer.clear()
        self._output_truncated = False
        self._update_output_search_visibility()

    def _save_output(self, path: str | None = None) -> None:
        """Save the output panel contents to a text file."""
        # clicked signal passes a bool — normalize to None
        if not isinstance(path, str):
            path = None
        text = self.output.toPlainText()
        if not text.strip():
            self.statusBar().showMessage("No output to save")
            return

        if path is None:
            tool_name = self.data["tool"] if self.data else "output"
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"{tool_name}_output_{stamp}.txt"
            default_path = str(Path.home() / default_name)
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Output", default_path,
                "Text Files (*.txt);;All Files (*)"
            )
        if not path:
            return

        try:
            Path(path).write_text(text, encoding="utf-8")
            self.statusBar().showMessage(f"Output saved to {path}")
        except OSError as e:
            self.statusBar().showMessage(f"Error saving output: {e}")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def print_prompt() -> None:
    """Print the LLM schema-generation prompt from SCHEMA_PROMPT.txt to stdout."""
    prompt_path = _bundled_root() / "SCHEMA_PROMPT.txt"
    if not prompt_path.exists():
        print("Error: SCHEMA_PROMPT.txt not found in scaffold_data/", file=sys.stderr)
        sys.exit(1)
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Error reading SCHEMA_PROMPT.txt: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


def print_preset_prompt() -> None:
    """Print the LLM preset-generation prompt from PRESET_PROMPT.txt to stdout."""
    prompt_path = _bundled_root() / "PRESET_PROMPT.txt"
    if not prompt_path.exists():
        print("Error: PRESET_PROMPT.txt not found in scaffold_data/", file=sys.stderr)
        sys.exit(1)
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Error reading PRESET_PROMPT.txt: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


def main() -> None:
    """Entry point: handle --help / --prompt / --validate CLI flags, then launch the GUI."""
    multiprocessing.freeze_support()
    atexit.register(_reset_regex_pool)
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"Scaffold {__version__}")
        sys.exit(0)

    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            f"Scaffold {__version__} - CLI-to-GUI Translation Layer\n"
            "\n"
            "Usage:\n"
            "  python scaffold.py                        Launch the tool picker GUI\n"
            "  python scaffold.py <tool.json>            Open a specific tool directly\n"
            "  python scaffold.py --validate <tool.json> Validate a schema (no GUI)\n"
            "  python scaffold.py --prompt               Print the LLM schema-generation prompt\n"
            "  python scaffold.py --preset-prompt          Print the LLM preset-generation prompt\n"
            "  python scaffold.py --version              Show version and exit\n"
            "  python scaffold.py --help                 Show this help and exit\n"
            "\n"
            "  python scaffold.py --run-cascade <cascade.json>  Run a saved cascade headlessly\n"
            "                     [--var name=value]              Supply a variable (repeatable)\n"
            "                     [--vars file.json]              Load variables from JSON file\n"
            "                     [--loop N]                      Run the cascade N times (1-1000); required\n"
            "                                                     when the cascade's saved loop_mode is on,\n"
            "                                                     otherwise defaults to 1\n"
            "                     [--summary path]                Write JSON summary to path\n"
            "\n"
            "Portable mode:  Place portable.txt next to scaffold.py to store settings locally\n"
        )
        sys.exit(0)

    if "--prompt" in sys.argv:
        print_prompt()
        sys.exit(0)

    if "--preset-prompt" in sys.argv:
        print_preset_prompt()
        sys.exit(0)

    if "--validate" in sys.argv:
        idx = sys.argv.index("--validate")
        if idx + 1 >= len(sys.argv):
            print("Usage: python scaffold.py --validate <tool.json>", file=sys.stderr)
            sys.exit(1)
        path = sys.argv[idx + 1]

        try:
            data = load_tool(path)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        errors = validate_tool(data)
        if errors:
            print(f"Validation failed for {path}:")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        else:
            data = normalize_tool(data)
            arg_count = len(data.get("arguments", []))
            sub_count = len(data.get("subcommands") or [])
            for sub in data.get("subcommands") or []:
                arg_count += len(sub.get("arguments", []))
            print(f"Valid: {data['tool']} ({arg_count} arguments, {sub_count} subcommands)")
            sys.exit(0)

    if "--run-cascade" in sys.argv:
        parsed, err = _parse_run_cascade_args(sys.argv[1:])
        if err is not None:
            print(f"Usage error: {err}", file=sys.stderr)
            print(
                "  python scaffold.py --run-cascade <cascade.json>\n"
                "                     [--var name=value] [--vars file.json]\n"
                "                     [--loop N] [--summary path]",
                file=sys.stderr,
            )
            sys.exit(1)

        # Peek at the cascade's loop_mode so we can require an explicit --loop N
        # when it is set. File-read, JSON-parse, and wrong-format failures are
        # all deferred to the runner's existing error handling — the peek must
        # not duplicate those error paths or the user would see a misleading
        # "specify --loop N" message for a non-cascade file.
        loop_mode_in_file = False
        try:
            _peek = json.loads(Path(parsed["cascade_path"]).read_text(encoding="utf-8"))
            if isinstance(_peek, dict) and _peek.get("_format") == "scaffold_cascade":
                loop_mode_in_file = bool(_peek.get("loop_mode", False))
        except (OSError, json.JSONDecodeError):
            pass

        loop_explicit = parsed["loop_count"] is not None
        if loop_mode_in_file and not loop_explicit:
            print(
                "Error: cascade has loop_mode enabled; specify --loop N to set "
                "the iteration count.\n"
                "The cascade's saved loop_mode flag is honored in GUI mode but "
                "headless mode\nrequires an explicit count to prevent infinite "
                "loops in automation contexts.",
                file=sys.stderr,
            )
            sys.exit(1)

        loop_count = parsed["loop_count"] if loop_explicit else 1
        runner = CascadeHeadlessRunner(
            cascade_path=parsed["cascade_path"],
            variables=parsed["variables"],
            loop_count=loop_count,
            summary_path=parsed["summary_path"],
        )
        sys.exit(runner.run())

    # GUI launch
    app = QApplication(sys.argv)

    # Apply theme from settings or system detection
    settings = _create_settings()
    theme_pref = settings.value("appearance/theme", "system")
    if theme_pref == "dark":
        apply_theme(True)
    elif theme_pref == "light":
        apply_theme(False)
    else:
        apply_theme(_detect_system_dark())

    # Direct launch with a JSON path
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        path = args[0]
        try:
            data = load_tool(path)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        errors = validate_tool(data)
        if errors:
            print(f"Validation failed for {path}:")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)

        window = MainWindow(tool_path=path)
    else:
        # No args — show tool picker
        window = MainWindow()

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
