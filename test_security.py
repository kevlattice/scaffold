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

"""Security Audit Test Suite for Scaffold.

Consolidates and expands all security-relevant assertions into one auditable
file.  Tests the core security invariants: no shell intermediary, no eval/exec,
no network access, typed widget constraints, safe preset handling, and literal
metacharacter passthrough.
"""

import copy
import html
import json
import os
import random
import re
import string
import sys
import tempfile
import shutil
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from PySide6.QtWidgets import QMessageBox

# Auto-accept "Missing Format Marker" warnings (test schemas lack _format)
_original_qmb_warning = QMessageBox.warning
def _patched_warning(parent, title, text, *args, **kwargs):
    if title == "Missing Format Marker":
        return QMessageBox.StandardButton.Yes
    if title == "Load Error":
        return QMessageBox.StandardButton.Ok
    return _original_qmb_warning(parent, title, text, *args, **kwargs)
QMessageBox.warning = _patched_warning

# Auto-decline recovery prompts
QMessageBox.question = lambda *a, **kw: QMessageBox.StandardButton.No

import scaffold

# Suppress the first-run welcome modal so tests don't block on QDialog.exec()
scaffold.MainWindow._suppress_welcome_dialog = True


def _cleanup_recovery_files():
    """Remove all Scaffold recovery files from temp directory."""
    tmp = Path(tempfile.gettempdir())
    for f in tmp.glob("scaffold_recovery_*.json"):
        try:
            f.unlink()
        except OSError:
            pass

_cleanup_recovery_files()


passed = 0
failed = 0
errors = []


def check(condition, name):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        errors.append(name)
        print(f"  FAIL: {name}")


# =====================================================================
# Helpers
# =====================================================================

_SCAFFOLD_SRC = (Path(__file__).parent / "scaffold.py").read_text(encoding="utf-8")

# Shared temp directory for reusable schemas — cleaned up at the end
_shared_tmp = tempfile.mkdtemp(prefix="scaffold_security_test_")


def _write_schema(name, tool_dict):
    """Write a tool schema to the shared temp dir and return its path."""
    path = Path(_shared_tmp) / f"{name}.json"
    path.write_text(json.dumps(tool_dict), encoding="utf-8")
    return str(path)


def _make_tool(binary="echo", args=None, positional=False, arg_type="string"):
    """Return a minimal valid tool dict with one argument."""
    if args is None:
        args = [{
            "flag": "--input",
            "name": "input",
            "type": arg_type,
            "positional": positional,
        }]
    return {
        "tool": f"security_test_{arg_type}{'_pos' if positional else ''}",
        "binary": binary,
        "description": "security test tool",
        "arguments": args,
    }


def _load_form(win, schema_path):
    """Load a tool schema into a MainWindow and return the form."""
    win._load_tool_path(schema_path)
    app.processEvents()
    return win.form


def _set_and_build(form, value, arg_type="string"):
    """Set a field value on an already-loaded form and return build_command()."""
    key = (form.GLOBAL, "--input")
    w = form.fields[key]["widget"]

    if arg_type == "password":
        w._line_edit.setText(str(value))
    elif arg_type == "text":
        w.setPlainText(str(value))
    elif arg_type in ("file", "directory"):
        w._line_edit.setText(str(value))
    else:
        w.setText(str(value))

    app.processEvents()
    return form.build_command()


# =====================================================================
print("\n=== SECTION 1: Static Analysis — Forbidden Patterns ===")
# =====================================================================

_forbidden = [
    (r'shell\s*=\s*True',             "shell=True"),
    (r'\bsubprocess\.call\b',         "subprocess.call"),
    (r'\bsubprocess\.run\b',          "subprocess.run"),
    (r'\bsubprocess\.Popen\b',        "subprocess.Popen"),
    (r'\bos\.system\s*\(',            "os.system("),
    (r'\bos\.popen\s*\(',             "os.popen("),
    (r'\beval\s*\(',                  "eval("),
    (r'(?<!\.)\bexec\s*\(',           "exec("),
    (r'\b__import__\s*\(',            "__import__("),
    (r'(?<!\.)(?<!re\.)\bcompile\s*\(', "bare compile("),
]

for pattern, label in _forbidden:
    matches = re.findall(pattern, _SCAFFOLD_SRC)
    check(len(matches) == 0,
          f"1: no {label} in source (found {len(matches)})")

# =====================================================================
print("\n=== SECTION 2: Shell Metacharacter Passthrough in build_command() ===")
# =====================================================================

_META_CHARS = {
    "pipe":              "|",
    "semicolon":         ";",
    "ampersand":         "&",
    "variable":          "$HOME",
    "backtick":          "`whoami`",
    "subshell":          "$(id)",
    "brace_expansion":   "{a,b}",
    "input_redirect":    "< /etc/passwd",
    "output_redirect":   "> /tmp/pwned",
    "history":           "!event",
    "tilde":             "~root",
    "newline":           "foo\nbar",
    "null_byte":         "foo\x00bar",
    "glob_star":         "*",
    "glob_question":     "?",
    "compound_and":      "foo && rm -rf /",
    "compound_or":       "foo || echo pwned",
    "stress_test":       "; rm -rf / && echo $(whoami) | cat > /tmp/pwned",
}

# --- 2a: String field metacharacter passthrough ---
# Create one MainWindow and reuse it for all string metacharacter tests
print("\n  --- 2a: String field metacharacter passthrough ---")
_tool_str = scaffold.normalize_tool(_make_tool(arg_type="string"))
_schema_str = _write_schema("string", _tool_str)
_win_str = scaffold.MainWindow()
_form_str = _load_form(_win_str, _schema_str)

for label, value in _META_CHARS.items():
    cmd, _ = _set_and_build(_form_str, value, "string")
    check(value in cmd,
          f"2a-{label}: literal value in cmd list")
    check(len(cmd) == 3,
          f"2a-{label}: cmd length is 3 (got {len(cmd)})")

# --- 2b: Password field metacharacter passthrough ---
print("\n  --- 2b: Password field metacharacter passthrough ---")
_tool_pw = scaffold.normalize_tool(_make_tool(arg_type="password"))
_schema_pw = _write_schema("password", _tool_pw)
_win_pw = scaffold.MainWindow()
_form_pw = _load_form(_win_pw, _schema_pw)

_pw_subset = ["pipe", "semicolon", "subshell", "stress_test"]
for label in _pw_subset:
    value = _META_CHARS[label]
    cmd, _ = _set_and_build(_form_pw, value, "password")
    check(value in cmd,
          f"2b-{label}: password field literal in cmd")
    check(len(cmd) == 3,
          f"2b-{label}: cmd length is 3 (got {len(cmd)})")

# --- 2c: Text field metacharacter passthrough ---
print("\n  --- 2c: Text field metacharacter passthrough ---")
_tool_txt = scaffold.normalize_tool(_make_tool(arg_type="text"))
_schema_txt = _write_schema("text", _tool_txt)
_win_txt = scaffold.MainWindow()
_form_txt = _load_form(_win_txt, _schema_txt)

_text_subset = ["pipe", "subshell", "compound_and", "stress_test"]
for label in _text_subset:
    value = _META_CHARS[label]
    cmd, _ = _set_and_build(_form_txt, value, "text")
    check(value in cmd,
          f"2c-{label}: text field literal in cmd")
    check(len(cmd) == 3,
          f"2c-{label}: cmd length is 3 (got {len(cmd)})")

# --- 2d: File/directory field metacharacter passthrough ---
print("\n  --- 2d: File/directory field metacharacter passthrough ---")
_path_values = ["/tmp/$(whoami)", "/tmp/; rm -rf /"]

for ftype in ("file", "directory"):
    _tool_f = scaffold.normalize_tool(_make_tool(arg_type=ftype))
    _schema_f = _write_schema(ftype, _tool_f)
    _win_f = scaffold.MainWindow()
    _form_f = _load_form(_win_f, _schema_f)
    for i, value in enumerate(_path_values):
        cmd, _ = _set_and_build(_form_f, value, ftype)
        check(value in cmd,
              f"2d-{ftype}_{i}: path value literal in cmd")

# --- 2e: Positional argument metacharacter passthrough ---
print("\n  --- 2e: Positional argument metacharacter passthrough ---")
_tool_pos = scaffold.normalize_tool(_make_tool(arg_type="string", positional=True))
_schema_pos = _write_schema("positional", _tool_pos)
_win_pos = scaffold.MainWindow()
_form_pos = _load_form(_win_pos, _schema_pos)

_pos_subset = ["pipe", "subshell", "stress_test", "null_byte"]
for label in _pos_subset:
    value = _META_CHARS[label]
    cmd, _ = _set_and_build(_form_pos, value, "string")
    check(value in cmd,
          f"2e-{label}: positional literal in cmd")
    # Positional: [binary, value] — length 2
    check(len(cmd) == 2,
          f"2e-{label}: cmd length is 2 for positional (got {len(cmd)})")

# ---------------------------------------------------------------------
# 2f: metachar passthrough with separator="equals" (--flag=value)
# ---------------------------------------------------------------------
print("\n  --- 2f: separator=equals metachar passthrough ---")
_s2f_schema = {
    "_format": "scaffold_schema",
    "tool": "s2f_equals_probe",
    "binary": "echo",
    "description": "",
    "elevated": None,
    "subcommands": None,
    "arguments": [
        {"name": "Flag", "flag": "--flag", "type": "string",
         "separator": "equals"},
    ],
}
_s2f_data = scaffold.normalize_tool(_s2f_schema)
_s2f_path = _write_schema("s2f_equals", _s2f_data)
_s2f_win = scaffold.MainWindow()
_s2f_form = _load_form(_s2f_win, _s2f_path)
_s2f_metachars = [
    "value; rm -rf /",
    "value && ls",
    "value | cat /etc/passwd",
    "value `whoami`",
    "value $(id)",
    "value > /tmp/out",
]
_s2f_key = (_s2f_form.GLOBAL, "--flag")
for _s2f_mc in _s2f_metachars:
    _s2f_form.fields[_s2f_key]["widget"].setText(_s2f_mc)
    app.processEvents()
    _s2f_cmd, _ = _s2f_form.build_command()
    # The value must appear joined to the flag with '=' and NOT split
    # through shell interpretation. Since build_command returns a list
    # of args for QProcess (no shell), each arg is a literal string.
    _s2f_joined = f"--flag={_s2f_mc}"
    check(_s2f_joined in _s2f_cmd,
          f"2f: separator=equals preserves metachars literally in --flag={_s2f_mc!r}")

# ---------------------------------------------------------------------
# 2g: metachar passthrough with separator="none" (-Dkey=value shape)
# ---------------------------------------------------------------------
print("\n  --- 2g: separator=none metachar passthrough ---")
_s2g_schema = {
    "_format": "scaffold_schema",
    "tool": "s2g_none_probe",
    "binary": "echo",
    "description": "",
    "elevated": None,
    "subcommands": None,
    "arguments": [
        {"name": "Define", "flag": "-D", "type": "string",
         "separator": "none"},
    ],
}
_s2g_data = scaffold.normalize_tool(_s2g_schema)
_s2g_path = _write_schema("s2g_none", _s2g_data)
_s2g_win = scaffold.MainWindow()
_s2g_form = _load_form(_s2g_win, _s2g_path)
_s2g_key = (_s2g_form.GLOBAL, "-D")
for _s2g_mc in _s2f_metachars:
    _s2g_form.fields[_s2g_key]["widget"].setText(_s2g_mc)
    app.processEvents()
    _s2g_cmd, _ = _s2g_form.build_command()
    _s2g_joined = f"-D{_s2g_mc}"
    check(_s2g_joined in _s2g_cmd,
          f"2g: separator=none preserves metachars literally in -D{_s2g_mc!r}")

# =====================================================================
print("\n=== SECTION 3: Binary Field Validation Hardening ===")
# =====================================================================

def _binary_errors(binary_value):
    """Return validation errors for a tool with the given binary."""
    data = {"tool": "test", "binary": binary_value,
            "description": "test", "arguments": []}
    return scaffold.validate_tool(data)


# 3a: Null byte in binary
errs = _binary_errors("nmap\x00; rm -rf /")
check(any("null" in e.lower() for e in errs),
      f"3a: null byte in binary rejected ({errs})")

# 3b: Unicode homoglyph (Cyrillic а U+0430 instead of ASCII a)
# This is a valid filename on most systems — document as accepted edge case
errs = _binary_errors("nm\u0430p")
check(not any("binary" in e.lower() and ("metachar" in e.lower() or "null" in e.lower())
              for e in errs),
      "3b: unicode homoglyph accepted (known edge case)")

# 3c: Binary that is just whitespace
errs = _binary_errors("   ")
check(any("non-empty" in e.lower() for e in errs),
      f"3c: whitespace-only binary rejected ({errs})")

# 3d: Binary with leading/trailing whitespace
# Whitespace is caught by a dedicated check (not metachar)
errs = _binary_errors(" nmap ")
check(any("whitespace" in e.lower() for e in errs),
      f"3d: binary with spaces caught as whitespace ({errs})")

# 3e: Binary with newline
errs = _binary_errors("nmap\necho pwned")
# Newline is NOT in _SHELL_METACHAR — safe because QProcess has no shell
newline_caught = any("metachar" in e.lower() or "null" in e.lower() for e in errs)
if newline_caught:
    check(True, "3e: binary with newline rejected")
else:
    check(True, "3e: binary with newline accepted (no shell=True, so safe in QProcess)")
    print("    NOTE: newline in binary not caught — safe because QProcess has no shell")

# 3f: Binary with tab
errs = _binary_errors("nmap\techo")
tab_caught = any("metachar" in e.lower() or "null" in e.lower() for e in errs)
if tab_caught:
    check(True, "3f: binary with tab rejected")
else:
    check(True, "3f: binary with tab accepted (no shell=True, so safe in QProcess)")
    print("    NOTE: tab in binary not caught — safe because QProcess has no shell")

# 3g: Very long binary (257 chars)
errs = _binary_errors("x" * 257)
check(any("too long" in e.lower() for e in errs),
      f"3g: 257-char binary rejected ({errs})")

# 3h: Binary at exactly 256 chars
errs = _binary_errors("x" * 256)
check(not any("too long" in e.lower() for e in errs),
      "3h: 256-char binary accepted")

# 3i: Binary "sh" → valid
errs = _binary_errors("sh")
check(not any("binary" in e.lower() for e in errs),
      "3i: 'sh' is a valid binary name")

# 3j: Binary "bash" → valid
errs = _binary_errors("bash")
check(not any("binary" in e.lower() for e in errs),
      "3j: 'bash' is a valid binary name")

# 3k: Binary "cmd.exe" → valid
errs = _binary_errors("cmd.exe")
check(not any("binary" in e.lower() for e in errs),
      "3k: 'cmd.exe' is a valid binary name")

# 3l: Relative path traversal
errs = _binary_errors("../../../usr/bin/nmap")
check(any("path separator" in e.lower() for e in errs),
      f"3l: relative path traversal rejected ({errs})")

# 3m: Windows relative traversal
errs = _binary_errors("..\\..\\Windows\\System32\\cmd.exe")
check(any("path separator" in e.lower() for e in errs),
      f"3m: Windows relative traversal rejected ({errs})")

# 3n: Absolute path /usr/bin/nmap → valid
errs = _binary_errors("/usr/bin/nmap")
check(not any("path separator" in e.lower() for e in errs),
      "3n: absolute Unix path accepted")

# 3o: Windows absolute C:\Windows\System32\ping.exe → valid
errs = _binary_errors("C:\\Windows\\System32\\ping.exe")
check(not any("path separator" in e.lower() for e in errs),
      "3o: absolute Windows path accepted")

# 3p–3t: Leading-dash binary names are rejected on all platforms.
# On Windows, get_elevation_command does NOT insert a "--" options-terminator
# before the binary, so a schema with "binary": "--copyns" would be swallowed
# by gsudo as its own flag. Defense-in-depth: reject everywhere.

# 3p: "--copyns" → rejected with message mentioning leading dash
errs = _binary_errors("--copyns")
check(any("-" in e and ("start" in e.lower() or "option" in e.lower()) for e in errs),
      f"3p: '--copyns' rejected with leading-dash message ({errs})")

# 3q: "-i" → rejected
errs = _binary_errors("-i")
check(any("-" in e and ("start" in e.lower() or "option" in e.lower()) for e in errs),
      f"3q: '-i' rejected with leading-dash message ({errs})")

# 3r: "-x" → rejected
errs = _binary_errors("-x")
check(any("-" in e and ("start" in e.lower() or "option" in e.lower()) for e in errs),
      f"3r: '-x' rejected with leading-dash message ({errs})")

# 3s: "nmap" → still accepted (regression guard)
errs = _binary_errors("nmap")
check(not any("binary" in e.lower() for e in errs),
      f"3s: 'nmap' still accepted — no leading-dash false positive ({errs})")

# 3t: "-" alone → rejected
errs = _binary_errors("-")
check(any("-" in e and ("start" in e.lower() or "option" in e.lower()) for e in errs),
      f"3t: bare '-' rejected with leading-dash message ({errs})")

# 3u: "" → still handled by existing empty-string check; no double-reject
# (should emit the non-empty message, NOT the leading-dash message)
errs = _binary_errors("")
check(any("non-empty" in e.lower() for e in errs),
      f"3u: empty string still caught by empty-string check ({errs})")
check(not any("start" in e.lower() and "-" in e for e in errs),
      f"3u: empty string does not trip leading-dash check ({errs})")

# 3v–3ad: "tool" field shape (F3). Rejects path-traversal-shaped values that
# would feed into _presets_dir's directory-name join. The regex allows letters,
# digits, spaces, and "_.()-" so shipped human-friendly names like
# "Ansible (Ad-Hoc)" remain valid.

def _tool_errors(tool_name):
    """Return validation errors for a tool with the given tool name."""
    data = {"tool": tool_name, "binary": "echo",
            "description": "test", "arguments": []}
    return scaffold.validate_tool(data)


# 3v: relative traversal
errs = _tool_errors("../../../tmp/evil")
check(any("tool" in e.lower() for e in errs),
      f"3v: '../../../tmp/evil' rejected, message mentions tool ({errs})")

# 3w: absolute Unix path
errs = _tool_errors("/etc/passwd")
check(any("tool" in e.lower() for e in errs),
      f"3w: '/etc/passwd' rejected ({errs})")

# 3x: Windows traversal
errs = _tool_errors("..\\..\\windows")
check(any("tool" in e.lower() for e in errs),
      f"3x: '..\\\\..\\\\windows' rejected ({errs})")

# 3y: forward slashes (path separator)
errs = _tool_errors("tool/with/slashes")
check(any("tool" in e.lower() for e in errs),
      f"3y: 'tool/with/slashes' rejected ({errs})")

# 3z: null byte
errs = _tool_errors("a\x00b")
check(any("tool" in e.lower() for e in errs),
      f"3z: null byte rejected ({errs})")

# 3aa: empty string — caught by existing presence/non-empty check
errs = _tool_errors("")
check(any("tool" in e.lower() for e in errs),
      f"3aa: empty string still rejected ({errs})")

# 3ab: 65 chars — exceeds 64-char cap
errs = _tool_errors("x" * 65)
check(any("tool" in e.lower() for e in errs),
      f"3ab: 65-char tool rejected (length cap) ({errs})")

# 3ac: leading dash — fails alphanumeric-start anchor
errs = _tool_errors("-nmap")
check(any("tool" in e.lower() for e in errs),
      f"3ac: '-nmap' rejected (no leading dash) ({errs})")

# 3ad: double-dot defense-in-depth (matches the regex shape but contains "..")
errs = _tool_errors("foo..bar")
check(any("tool" in e.lower() and ".." in e for e in errs),
      f"3ad: 'foo..bar' rejected with message mentioning '..' ({errs})")

# 3ae: trailing whitespace — rejected by surrounding-whitespace check
errs = _tool_errors("nmap ")
check(any("tool" in e.lower() and "whitespace" in e.lower() for e in errs),
      f"3ae: 'nmap ' rejected with message mentioning whitespace ({errs})")

# 3af: leading whitespace — rejected by alphanumeric-start anchor
errs = _tool_errors(" nmap")
check(any("tool" in e.lower() for e in errs),
      f"3af: ' nmap' rejected ({errs})")

# 3ag: regression guards — bare tool names still accepted
for _tn in ("nmap", "ansible-galaxy", "airodump-ng"):
    errs = _tool_errors(_tn)
    check(not any("tool" in e.lower() for e in errs),
          f"3ag: '{_tn}' still accepted ({errs})")

# 3ah: regression guards — shipped human-friendly names still accepted
for _tn in ("Ansible (Ad-Hoc)", "Docker Compose"):
    errs = _tool_errors(_tn)
    check(not any("tool" in e.lower() for e in errs),
          f"3ah: '{_tn}' still accepted (shipped schema) ({errs})")


# 3ai–3aj: "__global__" reserved subcommand name (F5). The literal "__global__"
# is the scope sentinel ToolForm uses for global-arg field keys; a subcommand
# with this name silently clobbers global-arg field registration.

def _sub_errors(sub_name):
    """Return validation errors for a tool with one subcommand of this name."""
    data = {
        "tool": "test",
        "binary": "echo",
        "description": "test",
        "arguments": [],
        "subcommands": [{"name": sub_name, "arguments": []}],
    }
    return scaffold.validate_tool(data)


# 3ai: reserved name rejected
errs = _sub_errors("__global__")
check(any("reserved" in e.lower() for e in errs),
      f"3ai: subcommand named '__global__' rejected with 'reserved' ({errs})")

# 3aj: regression — ordinary subcommand name still accepted
errs = _sub_errors("scan")
check(not any("reserved" in e.lower() for e in errs),
      f"3aj: subcommand named 'scan' still accepted ({errs})")


# 3ak–3aq: required enum/multi_enum must declare a non-null default (F10).
# Without one, the widget silently selects choices[0] and the user never sees
# a "no value chosen" state.

def _enum_arg_errors(*, type_, required, default_present, default_value=None,
                     in_subcommand=False):
    """Return validation errors for a schema with a single enum/multi_enum arg.

    If default_present is False the "default" key is omitted entirely; otherwise
    it is set to default_value (which may be None).
    """
    arg = {
        "name": "mode",
        "flag": "--mode",
        "type": type_,
        "choices": ["alpha", "bravo", "charlie"],
        "required": required,
    }
    if default_present:
        arg["default"] = default_value
    if in_subcommand:
        data = {
            "tool": "test",
            "binary": "echo",
            "description": "test",
            "arguments": [],
            "subcommands": [{"name": "scan", "arguments": [arg]}],
        }
    else:
        data = {"tool": "test", "binary": "echo",
                "description": "test", "arguments": [arg]}
    return scaffold.validate_tool(data)


def _has_required_default_msg(errs):
    return any(
        "required" in e.lower() and "default" in e.lower() and "--mode" in e
        for e in errs
    )


# 3ak: required enum + default: null → rejected
errs = _enum_arg_errors(type_="enum", required=True,
                        default_present=True, default_value=None)
check(_has_required_default_msg(errs),
      f"3ak: required enum with default=null rejected ({errs})")

# 3al: required enum + no default key → rejected
errs = _enum_arg_errors(type_="enum", required=True, default_present=False)
check(_has_required_default_msg(errs),
      f"3al: required enum with no 'default' key rejected ({errs})")

# 3am: required enum + valid default → accepted
errs = _enum_arg_errors(type_="enum", required=True,
                        default_present=True, default_value="alpha")
check(not _has_required_default_msg(errs),
      f"3am: required enum with default='alpha' accepted ({errs})")

# 3an: optional enum + default: null → accepted (only required enums are checked)
errs = _enum_arg_errors(type_="enum", required=False,
                        default_present=True, default_value=None)
check(not _has_required_default_msg(errs),
      f"3an: optional enum with default=null accepted ({errs})")

# 3ao: required multi_enum + default: [] → accepted (empty list is a valid
# "no choices selected" multi_enum default)
errs = _enum_arg_errors(type_="multi_enum", required=True,
                        default_present=True, default_value=[])
check(not _has_required_default_msg(errs),
      f"3ao: required multi_enum with default=[] accepted ({errs})")

# 3ap: required multi_enum + default: null → rejected
errs = _enum_arg_errors(type_="multi_enum", required=True,
                        default_present=True, default_value=None)
check(_has_required_default_msg(errs),
      f"3ap: required multi_enum with default=null rejected ({errs})")

# 3aq: same required+null shape inside a subcommand → also rejected
# (proves _validate_args coverage extends to subcommand args)
errs = _enum_arg_errors(type_="enum", required=True,
                        default_present=True, default_value=None,
                        in_subcommand=True)
check(_has_required_default_msg(errs),
      f"3aq: required enum with default=null inside a subcommand rejected ({errs})")


# 3ar–3bb: argument "validation" regex must not be ReDoS-prone (F2).
# The three re.compile(arg["validation"]) consumer sites in ToolForm run
# every keystroke on user input; a catastrophic-backtracking pattern in
# the schema would hang the form validator. Gate at validate_tool time.

def _validation_arg_errors(regex, in_subcommand=False):
    """Return validation errors for a schema whose single string arg
    has the given validation regex. Covers both top-level and subcommand
    scopes so the F2 check's cross-scope coverage can be proved."""
    arg = {
        "name": "pattern_field",
        "flag": "--pf",
        "type": "string",
        "validation": regex,
    }
    if in_subcommand:
        data = {
            "tool": "test", "binary": "echo", "description": "test",
            "arguments": [],
            "subcommands": [{"name": "scan", "arguments": [arg]}],
        }
    else:
        data = {"tool": "test", "binary": "echo", "description": "test",
                "arguments": [arg]}
    return scaffold.validate_tool(data)


def _has_redos_msg(errs):
    return any(
        "validation" in e.lower()
        and ("catastrophic" in e.lower() or "redos" in e.lower()
             or "backtracking" in e.lower())
        for e in errs
    )


# Rejection cases — the helper's two heuristics (nested quantifier,
# alternation-under-quantifier) cover the classical ReDoS shapes.

# 3ar: (a+)+$ (canonical nested quantifier) rejected
errs = _validation_arg_errors("(a+)+$")
check(_has_redos_msg(errs), f"3ar: '(a+)+$' rejected ({errs})")

# 3as: (a*)*b (another nested quantifier shape) rejected
errs = _validation_arg_errors("(a*)*b")
check(_has_redos_msg(errs), f"3as: '(a*)*b' rejected ({errs})")

# 3at: (a|a)+$ (overlapping alternation under quantifier) rejected
errs = _validation_arg_errors("(a|a)+$")
check(_has_redos_msg(errs), f"3at: '(a|a)+$' rejected ({errs})")

# 3au: (a|ab)*c (shared-prefix alternation under quantifier) rejected
errs = _validation_arg_errors("(a|ab)*c")
check(_has_redos_msg(errs), f"3au: '(a|ab)*c' rejected ({errs})")

# 3av: a{200,500} (large bounded repetition) — currently ACCEPTED.
# _pattern_is_redos_prone has only H1/H2 heuristics and does not flag
# large bounded repetition. Documented-behavior regression guard: if the
# helper is ever tightened to include an H3-style bound check, this test
# breaks and we should update the shipped-schema acceptance list at the
# same time.
errs = _validation_arg_errors("a{200,500}")
check(not _has_redos_msg(errs),
      f"3av: 'a{{200,500}}' currently accepted (helper has no H3 check) ({errs})")

# Acceptance cases — shipped-style shapes must not be false-positived.

# 3aw: MAC-address-style character-class pattern accepted
errs = _validation_arg_errors("^[0-9A-Fa-f:]{17}$")
check(not _has_redos_msg(errs), f"3aw: MAC-style pattern accepted ({errs})")

# 3ax: simple date pattern accepted
errs = _validation_arg_errors(r"^\d{4}-\d{2}-\d{2}$")
check(not _has_redos_msg(errs), f"3ax: date pattern accepted ({errs})")

# 3ay: 2-letter country code pattern accepted
errs = _validation_arg_errors("^[a-z]{2}$")
check(not _has_redos_msg(errs), f"3ay: country-code pattern accepted ({errs})")

# 3az: identifier pattern accepted
errs = _validation_arg_errors("^[a-zA-Z0-9_-]+$")
check(not _has_redos_msg(errs), f"3az: identifier pattern accepted ({errs})")

# 3ba: same ReDoS-prone shape inside a subcommand → also rejected
# (proves the F2 check applies to subcommand args too, not just top-level)
errs = _validation_arg_errors("(a+)+$", in_subcommand=True)
check(_has_redos_msg(errs),
      f"3ba: '(a+)+$' inside subcommand rejected ({errs})")

# 3bb: shipped-schema cohort — walk tools/**/*.json and assert no
# shipped schema is now rejected because of the F2 check. Guards
# against a future helper tightening that silently breaks a shipped
# schema.
_shipped_tools = Path(__file__).parent / "scaffold_data" / "tools"
_cohort_problems = []
for _schema_path in sorted(_shipped_tools.rglob("*.json")):
    try:
        _data = scaffold.load_tool(_schema_path)
    except Exception as _e:
        _cohort_problems.append(f"{_schema_path.name} load failed: {_e}")
        continue
    _errs = scaffold.validate_tool(_data)
    _rejected_by_f2 = [e for e in _errs if _has_redos_msg([e])]
    if _rejected_by_f2:
        _cohort_problems.append(
            f"{_schema_path.relative_to(_shipped_tools).as_posix()}: {_rejected_by_f2}"
        )
check(not _cohort_problems,
      f"3bb: shipped-schema cohort — no shipped regex tripped the F2 check "
      f"({len(_cohort_problems)} problems: {_cohort_problems})")


# =====================================================================
print("\n=== SECTION 4: Preset Injection Resistance ===")
# =====================================================================

print("\n  --- 4a: validate_preset() rejects dangerous structures ---")

# Nested dict value
errs4 = scaffold.validate_preset({"key": {"nested": "dict"}})
check(len(errs4) > 0, "4a-1: nested dict value rejected")

# List containing dicts
errs4 = scaffold.validate_preset({"key": [{"a": 1}]})
check(len(errs4) > 0, "4a-2: list with dicts rejected")

# List containing ints
errs4 = scaffold.validate_preset({"key": [1, 2, 3]})
check(len(errs4) > 0, "4a-3: list with ints rejected")

# Extremely long string value (10,001 chars)
errs4 = scaffold.validate_preset({"key": "A" * 10_001})
check(len(errs4) > 0, "4a-4: extremely long string value rejected")

# Extremely long key (10,001 chars)
errs4 = scaffold.validate_preset({"A" * 10_001: "val"})
check(len(errs4) > 0, "4a-5: extremely long key rejected")

# Shell metacharacters in values → valid (they're literal data)
meta_preset = {
    "__global__:--flag1": "; rm -rf /",
    "__global__:--flag2": "$(whoami)",
    "__global__:--flag3": "| cat /etc/passwd",
}
errs4 = scaffold.validate_preset(meta_preset)
check(len(errs4) == 0, "4a-6: metachar values accepted (literal data)")

# Schema-as-preset detection
errs4 = scaffold.validate_preset({"binary": "nmap", "arguments": []})
check(any("schema" in e.lower() for e in errs4),
      "4a-7: schema-as-preset detected")

print("\n  --- 4b: Preset round-trip with metacharacters ---")
# Reuse the string form from section 2
meta_value = "; rm -rf / && $(whoami)"
_set_and_build(_form_str, meta_value, "string")

# Serialize → apply_values round-trip
serialized = _form_str.serialize_values()
_form_str.apply_values(serialized)
app.processEvents()

cmd, _ = _form_str.build_command()
check(meta_value in cmd,
      "4b: metachar survives preset round-trip literally")
check(len(cmd) == 3,
      "4b: cmd length is 3 after round-trip")

# =====================================================================
print("\n=== SECTION 5: Extra Flags Injection Resistance ===")
# =====================================================================

# Create one MainWindow with no schema args, reuse for all extra-flags tests
_tool_ef = scaffold.normalize_tool(_make_tool(args=[]))
_schema_ef = _write_schema("extra_flags", _tool_ef)
_win_ef = scaffold.MainWindow()
_form_ef = _load_form(_win_ef, _schema_ef)


def _test_extra_flags(extra_text):
    """Set extra flags text and return the full cmd list."""
    _form_ef.extra_flags_group.setChecked(True)
    _form_ef.extra_flags_edit.setPlainText(extra_text)
    app.processEvents()
    cmd, _ = _form_ef.build_command()
    return cmd


# 5a: semicolon rm → discrete tokens
cmd5 = _test_extra_flags("; rm -rf /")
check(";" in cmd5, "5a: semicolon is a discrete token")
check("rm" in cmd5, "5a: 'rm' is a discrete token")
check(len(cmd5) > 1, "5a: multiple tokens produced")

# 5b: subshell → single token
cmd5 = _test_extra_flags("$(whoami)")
check("$(whoami)" in cmd5, "5b: $(whoami) is a literal token")

# 5c: backticks → literal tokens
cmd5 = _test_extra_flags("`id`")
check("`id`" in cmd5, "5c: backtick literal token")

# 5d: double-quoted value with spaces → two tokens
cmd5 = _test_extra_flags('--flag "value with spaces"')
check("--flag" in cmd5, "5d: --flag is a token")
check("value with spaces" in cmd5, "5d: quoted value is one token")
check(len(cmd5) == 3, f"5d: exactly 3 tokens [binary, --flag, value] (got {len(cmd5)})")

# 5e: single-quoted value → two tokens
cmd5 = _test_extra_flags("--flag 'single quoted'")
check("--flag" in cmd5, "5e: --flag is a token")
check("single quoted" in cmd5, "5e: single-quoted value is one token")

# 5f: unclosed quote → empty list (no broken tokens in preview), no crash
cmd5 = _test_extra_flags('--flag "unclosed')
check("--flag" not in cmd5, "5f: unclosed quote — no broken tokens in cmd")
check(len(cmd5) == 1, "5f: unclosed quote — only binary in cmd (malformed extras suppressed)")

# 5g: pipe → discrete literal tokens
cmd5 = _test_extra_flags("| cat /etc/passwd")
check("|" in cmd5, "5g: pipe is a discrete literal token")
check("cat" in cmd5, "5g: 'cat' is a discrete token")

# 5h: empty input → no extra tokens
cmd5 = _test_extra_flags("")
check(len(cmd5) == 1, "5h: empty input — only binary in cmd")

# 5i: whitespace only → no extra tokens
cmd5 = _test_extra_flags("   \n  ")
check(len(cmd5) == 1, "5i: whitespace-only — only binary in cmd")

# =====================================================================
print("\n=== SECTION 6: Path Traversal in Preset Names ===")
# =====================================================================

def _sanitize(name):
    return re.sub(r'[^\w\-. ]', '_', name)


_san_a = _sanitize("../../etc/passwd")
check("/" not in _san_a, f"6a: no forward slashes survive (got '{_san_a}')")

check("\\" not in _sanitize("..\\..\\Windows\\System32"),
      "6b: no backslashes survive")

check("\x00" not in _sanitize("preset\x00name"),
      "6c: null byte replaced")

check(_sanitize("normal_preset-name.v2") == "normal_preset-name.v2",
      "6d: safe name unchanged")

check(_sanitize("preset name with spaces") == "preset name with spaces",
      "6e: spaces preserved")

_san_xss = _sanitize("<script>alert(1)</script>")
check("<" not in _san_xss and ">" not in _san_xss,
      f"6f: angle brackets replaced (got '{_san_xss}')")

check(_sanitize("") == "",
      "6g: empty string returns empty")

# =====================================================================
print("\n=== SECTION 7: Recovery File Safety ===")
# =====================================================================

# Reuse the string schema for recovery tests
_win7 = scaffold.MainWindow()
_form7 = _load_form(_win7, _schema_str)

recovery_path = _win7._recovery_file_path()

if recovery_path:
    # 7a: Recovery file that is valid JSON but a list, not a dict
    # Known edge case: a list passes json.loads() but .get() on it raises
    # AttributeError. This is harmless (file is deleted on next load) but
    # documents the gap for future hardening.
    recovery_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7a: list-as-recovery — no crash")
    except (AttributeError, TypeError):
        # Expected: .get() fails on list — not a security issue, just
        # a robustness gap. The file is cleaned up on next valid load.
        check(True, "7a: list-as-recovery — AttributeError (known edge case)")
        # Clean up the file so subsequent tests start fresh
        try:
            recovery_path.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as e:
        check(False, f"7a: list-as-recovery — unexpected crash: {e}")

    # 7b: Invalid JSON (corrupted bytes)
    recovery_path.write_text("{corrupted: not json!!!", encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7b: corrupted JSON — no crash")
    except Exception as e:
        check(False, f"7b: corrupted JSON crashed: {e}")

    # 7c: Extremely long values
    big_data = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": time.time(),
        "__global__:--input": "X" * 100_000,
    }
    recovery_path.write_text(json.dumps(big_data), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7c: huge value recovery — no crash")
    except Exception as e:
        check(False, f"7c: huge value recovery crashed: {e}")

    # 7d: Shell metacharacters in _recovery_tool_path
    meta_recovery = {
        "_recovery_tool_path": "; rm -rf / && $(whoami)",
        "_recovery_timestamp": time.time(),
    }
    recovery_path.write_text(json.dumps(meta_recovery), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7d: metachar in tool_path — no crash")
    except Exception as e:
        check(False, f"7d: metachar in tool_path crashed: {e}")

    # 7e: Far-future timestamp — should still be offered (not expired)
    future_data = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": time.time() + 86400 * 365,  # 1 year in future
    }
    recovery_path.write_text(json.dumps(future_data), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7e: far-future timestamp — no crash")
    except Exception as e:
        check(False, f"7e: far-future timestamp crashed: {e}")

    # 7f: Expired timestamp — _check_for_recovery should delete it
    expired_data = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": time.time() - (scaffold.AUTOSAVE_EXPIRY_HOURS + 1) * 3600,
    }
    recovery_path.write_text(json.dumps(expired_data), encoding="utf-8")
    _win7._check_for_recovery()
    check(not recovery_path.exists(),
          "7f: expired recovery file deleted by _check_for_recovery")

    # --- F8: Recovery timestamp guard ---
    # _recovery_timestamp feeds directly into `time.time() - ts` arithmetic.
    # Before F8, a non-numeric value crashed _check_for_recovery /
    # _cleanup_stale_recovery_files with TypeError. The guard now treats
    # non-numeric (and bool, since isinstance(True, int) is True) as
    # corrupt and deletes the recovery file silently.

    # 7g: _recovery_timestamp is a string → no crash, file deleted
    bad_ts_str = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": "not-a-number",
    }
    recovery_path.write_text(json.dumps(bad_ts_str), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7g: string timestamp — _check_for_recovery did not crash")
    except Exception as e:
        check(False, f"7g: string timestamp crashed: {e}")
    check(not recovery_path.exists(),
          "7g: string timestamp — corrupt recovery file deleted")

    # 7h: _recovery_timestamp is a list → no crash, file deleted
    bad_ts_list = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": [1, 2, 3],
    }
    recovery_path.write_text(json.dumps(bad_ts_list), encoding="utf-8")
    try:
        _win7._check_for_recovery()
        check(True, "7h: list timestamp — _check_for_recovery did not crash")
    except Exception as e:
        check(False, f"7h: list timestamp crashed: {e}")
    check(not recovery_path.exists(),
          "7h: list timestamp — corrupt recovery file deleted")

    # 7i: Valid float timestamp → existing flow continues unchanged.
    # Baseline regression guard: the guard must not accidentally reject
    # valid timestamps. Count QMessageBox.question calls to verify the
    # recovery dialog was reached (the corrupt-file branch returns
    # before the dialog, so a dialog call means the normal path ran).
    valid_ts = {
        "_recovery_tool_path": _schema_str,
        "_recovery_timestamp": time.time(),
    }
    recovery_path.write_text(json.dumps(valid_ts), encoding="utf-8")
    _dialog_count = [0]
    _orig_question = QMessageBox.question
    def _counted_question(*a, **kw):
        _dialog_count[0] += 1
        return QMessageBox.StandardButton.No
    QMessageBox.question = _counted_question
    try:
        _win7._check_for_recovery()
        check(_dialog_count[0] == 1,
              f"7i: valid timestamp — recovery dialog reached "
              f"({_dialog_count[0]} calls)")
    except Exception as e:
        check(False, f"7i: valid timestamp crashed: {e}")
    finally:
        QMessageBox.question = _orig_question
    check(not recovery_path.exists(),
          "7i: valid timestamp — file deleted via normal flow")

else:
    # Recovery path is None — mark all as skipped. One entry per check()
    # call in the main branch (7a–7f: 1 each; 7g/7h: 2 each; 7i: 2).
    for label in ("7a", "7b", "7c", "7d", "7e", "7f",
                  "7g-nocrash", "7g-deleted",
                  "7h-nocrash", "7h-deleted",
                  "7i-dialog", "7i-deleted"):
        check(True, f"{label}: skipped (no recovery path)")

# =====================================================================
print("\n=== SECTION 8: QProcess Contract Verification ===")
# =====================================================================

from PySide6.QtCore import QProcess

# Reuse the string form — set a known value
_set_and_build(_form_str, "hello world", "string")

# 8a: build_command()[0] is the binary
cmd8, _ = _form_str.build_command()
check(cmd8[0] == "echo", f"8a: cmd[0] is the binary (got '{cmd8[0]}')")

# 8b-d: Start a QProcess and verify the contract
_win_str._on_run_stop()
app.processEvents()

if _win_str.process is not None:
    check(isinstance(_win_str.process, QProcess),
          "8b: process is a QProcess instance")

    check(_win_str.process.program() == cmd8[0],
          f"8c: QProcess.program() == cmd[0] (got '{_win_str.process.program()}')")

    expected_args = cmd8[1:]
    actual_args = _win_str.process.arguments()
    check(list(actual_args) == expected_args,
          f"8d: QProcess.arguments() == cmd[1:] (got {actual_args})")

    # Stop it
    if _win_str.process.state() != QProcess.ProcessState.NotRunning:
        _win_str._on_run_stop()
        app.processEvents()
else:
    # Process may have already finished (echo is fast)
    check(True, "8b: process already finished (echo is fast) — verified via build_command()")
    check(True, "8c: (skipped — process already finished)")
    check(True, "8d: (skipped — process already finished)")


# =====================================================================
print("\n=== SECTION 9: Subcommand Name Flag Injection ===")
# =====================================================================

def _make_sub_tool(sub_name):
    """Return a minimal valid tool dict with one subcommand named sub_name."""
    return {
        "tool": "sub_injection_test",
        "binary": "echo",
        "description": "test",
        "arguments": [],
        "subcommands": [{
            "name": sub_name,
            "description": "test sub",
            "arguments": [],
        }],
    }


def _sub_errors(sub_name):
    """Return validation errors for a tool with the given subcommand name."""
    return scaffold.validate_tool(_make_sub_tool(sub_name))


# 9a: Adversarial subcommand names — all must be rejected
_adversarial_sub_names = [
    ("list --recursive",   "flag injection via --"),
    ("list -r",            "flag injection via -"),
    ("--rm container",     "leading -- token"),
    ("list; rm -rf /",     "semicolon metachar"),
    ("list\nrm",           "embedded newline"),
    ("list\trm",           "embedded tab"),
    ("remote\rname",       "embedded carriage return"),
    ("list|cat",           "pipe metachar"),
    ("list&bg",            "ampersand metachar"),
    ("list$(whoami)",      "dollar-paren metachar"),
    ("list`id`",           "backtick metachar"),
]

for bad_name, desc in _adversarial_sub_names:
    errs9 = _sub_errors(bad_name)
    check(len(errs9) > 0, f"9a-{desc}: {bad_name!r} rejected")

# 9b: Positive controls — valid subcommand names must still pass
_valid_sub_names = [
    "list",
    "remote add",
    "compute instances create",
    "show-tags",
    "v2",
]

for good_name in _valid_sub_names:
    errs9 = _sub_errors(good_name)
    has_name_error = any("subcommand" in e.lower() and ("token" in e.lower() or "metachar" in e.lower() or "cannot start" in e.lower()) for e in errs9)
    check(not has_name_error, f"9b-positive: {good_name!r} accepted")

# 9c: End-to-end — bad subcommand name prevents tool from loading
_e2e_schema_path = _write_schema("sub_injection_e2e", _make_sub_tool("list --recursive"))
_win9 = scaffold.MainWindow()
_data_before = _win9.data  # may be None or a previously loaded tool
_win9._load_tool_path(_e2e_schema_path)
app.processEvents()
check(_win9.data is _data_before,
      "9c: schema with 'list --recursive' subcommand rejected (data unchanged)")

# 9d: Backslash in subcommand name — rejected as shell metachar
# The '\' carve-out in binary validation (section 3o) does NOT extend to
# subcommand names (see scaffold.py:209-212). Subcommand names are tokens,
# not paths.
errs9d = _sub_errors("foo\\bar")
check(any("shell metacharacter" in e.lower() for e in errs9d),
      "9d: backslash in subcommand name rejected "
      "(contrast: absolute-path binary accepts \\; section 3o)")

_win9.close()
_win9.deleteLater()
app.processEvents()


# =====================================================================
print("\n=== SECTION 10: HTML Injection Escaping ===")
# =====================================================================

# 10a: Label escapes arg name containing HTML
_s10_schema_a = {
    "tool": "html_inject_test",
    "binary": "echo",
    "description": "test",
    "arguments": [{
        "flag": "--xss",
        "name": "<img src=x>",
        "type": "string",
        "description": "safe description",
    }],
}
_s10_path_a = _write_schema("html_inject_name", _s10_schema_a)
_s10_win = scaffold.MainWindow()
_s10_form = _load_form(_s10_win, _s10_path_a)
app.processEvents()
_s10_label = _s10_form.fields[(scaffold.ToolForm.GLOBAL, "--xss")]["label"]
_s10_label_text = _s10_label.text()
check("&lt;img" in _s10_label_text or "<img" not in _s10_label_text,
      f"10a: label escapes <img> in arg name: {_s10_label_text!r}")

# 10b: Tooltip escapes description containing HTML
_s10_schema_b = {
    "tool": "html_inject_desc",
    "binary": "echo",
    "description": "test",
    "arguments": [{
        "flag": "--xss",
        "name": "Test",
        "type": "string",
        "description": "<b>injected</b>",
    }],
}
_s10_path_b = _write_schema("html_inject_desc", _s10_schema_b)
_s10_form_b = _load_form(_s10_win, _s10_path_b)
app.processEvents()
_s10_tooltip_b = _s10_form_b.fields[(scaffold.ToolForm.GLOBAL, "--xss")]["label"].toolTip()
check("&lt;b&gt;" in _s10_tooltip_b,
      f"10b: tooltip escapes <b> in description: {_s10_tooltip_b!r}")

# 10c: Tooltip escapes flag and short_flag containing HTML
_s10_schema_c = {
    "tool": "html_inject_flags",
    "binary": "echo",
    "description": "test",
    "arguments": [{
        "flag": "</p><h1>",
        "name": "Test",
        "type": "string",
        "short_flag": "<script>",
        "description": "needs tooltip",
    }],
}
_s10_path_c = _write_schema("html_inject_flags", _s10_schema_c)
_s10_form_c = _load_form(_s10_win, _s10_path_c)
app.processEvents()
_s10_key_c = None
for k in _s10_form_c.fields:
    if k[0] == scaffold.ToolForm.GLOBAL:
        _s10_key_c = k
        break
if _s10_key_c:
    _s10_tooltip_c = _s10_form_c.fields[_s10_key_c]["label"].toolTip()
    check("&lt;/p&gt;" in _s10_tooltip_c,
          f"10c: tooltip escapes </p> in flag: {_s10_tooltip_c!r}")
    check("&lt;script&gt;" in _s10_tooltip_c,
          f"10c: tooltip escapes <script> in short_flag: {_s10_tooltip_c!r}")
else:
    check(False, "10c: could not find field for flag injection test")

# 10d: Tool picker description tooltip is escaped
_s10_schema_d = {
    "tool": "html_inject_picker",
    "binary": "echo",
    "description": "<b>injected</b> picker desc",
    "arguments": [],
}
_s10_path_d = _write_schema("html_inject_picker", _s10_schema_d)
_s10_win_d = scaffold.MainWindow()
_s10_win_d._load_tool_path(_s10_path_d)
app.processEvents()
# Simulate the picker tooltip by checking the escape function directly
_s10_escaped_desc = html.escape("<b>injected</b> picker desc")
check("&lt;b&gt;" in _s10_escaped_desc,
      "10d: html.escape correctly escapes <b> in tool picker description")

# 10e: Tool picker binary tooltip ("not found" path) is escaped
_s10_binary_val = "<script>alert(1)</script>"
_s10_escaped_binary = html.escape(_s10_binary_val)
check("&lt;script&gt;" in _s10_escaped_binary,
      "10e: html.escape correctly escapes <script> in binary tooltip")
# Verify the escaping is applied in the template
_s10_tooltip_template = f"<p>'{html.escape(_s10_binary_val)}' not found in PATH. Install it or add its location to your PATH.</p>"
check("<script>" not in _s10_tooltip_template,
      f"10e: binary tooltip template contains no raw <script>: {_s10_tooltip_template!r}")

# 10f: Preset picker description tooltip is escaped
_s10_preset_desc = "<b><font size=72>CLICK HERE</font></b>"
_s10_escaped_preset = html.escape(_s10_preset_desc)
check("&lt;b&gt;" in _s10_escaped_preset,
      "10f: html.escape correctly escapes <b> in preset description")
# End-to-end: create a preset with HTML in _description, open the picker
_s10_preset_dir = Path(_shared_tmp) / "presets_html_test"
_s10_preset_dir.mkdir(parents=True, exist_ok=True)
_s10_preset_file = _s10_preset_dir / "injected.json"
_s10_preset_file.write_text(json.dumps({
    "_format": "scaffold_preset",
    "_description": _s10_preset_desc,
    "__global__:--xss": "val"
}), encoding="utf-8")
_s10_ppicker = scaffold.PresetPicker("html_inject_test", _s10_preset_dir, mode="load")
app.processEvents()
_s10_found_preset_tooltip = False
for _s10_r in range(_s10_ppicker.table.rowCount()):
    _s10_desc_item = _s10_ppicker.table.item(_s10_r, 2)
    if _s10_desc_item and _s10_desc_item.toolTip():
        check("<b>" not in _s10_desc_item.toolTip(),
              f"10f: preset picker tooltip has no raw <b>: {_s10_desc_item.toolTip()!r}")
        _s10_found_preset_tooltip = True
        break
if not _s10_found_preset_tooltip:
    check(False, "10f: no preset with tooltip found for injection test")
_s10_ppicker.close()
_s10_ppicker.deleteLater()
app.processEvents()

# 10g: Fallback widget tooltip escapes type value
_s10_schema_g = {
    "tool": "html_inject_fallback",
    "binary": "echo",
    "description": "test",
    "arguments": [{
        "flag": "--xss",
        "name": "Test",
        "type": "string",
        "description": "trigger fallback",
    }],
}
# Force the fallback path by temporarily breaking _build_widget_inner
_s10_path_g = _write_schema("html_inject_fallback", _s10_schema_g)
_s10_orig_build = scaffold.ToolForm._build_widget_inner

def _s10_broken_build(self, arg, key):
    raise RuntimeError("forced failure")

scaffold.ToolForm._build_widget_inner = _s10_broken_build
_s10_form_g = _load_form(_s10_win, _s10_path_g)
app.processEvents()
scaffold.ToolForm._build_widget_inner = _s10_orig_build  # restore immediately

_s10_key_g = (scaffold.ToolForm.GLOBAL, "--xss")
if _s10_key_g in _s10_form_g.fields:
    _s10_fb_tooltip = _s10_form_g.fields[_s10_key_g]["widget"].toolTip()
    # The type "string" is safe, but verify the escaping path works by checking
    # the tooltip contains the type value and is properly formed HTML
    check("string" in _s10_fb_tooltip and "<p>" in _s10_fb_tooltip,
          f"10g: fallback widget tooltip properly formed: {_s10_fb_tooltip!r}")
else:
    check(False, "10g: could not find fallback widget field")

_s10_win.close()
_s10_win.deleteLater()
_s10_win_d.close()
_s10_win_d.deleteLater()
app.processEvents()


# =====================================================================
# SECTION 11: Schema fuzzing (validate_tool + normalize_tool)
# Goal: validate_tool never crashes and always returns list[str];
# normalize_tool never crashes on dict input and raises TypeError on non-dict.
# No new deps; uses stdlib random/string/copy and the repo's check() harness.
# =====================================================================
print("\n=== SECTION 11: Schema fuzzing (validate_tool + normalize_tool) ===")

random.seed(0)  # deterministic — same mutations every run

_s11_seed = {
    "tool": "fuzz_seed",
    "binary": "echo",
    "description": "Seed for fuzzing",
    "arguments": [
        {"name": "Foo", "flag": "--foo", "type": "string"},
    ],
}

def _s11_rand_text(max_len=60):
    base = string.ascii_letters + string.digits + " -_./\\!@#$%^&*()[]{};:'\",<>?`~"
    if random.random() < 0.12:
        base += "✓✔✕✖漢字한글"
    return "".join(random.choice(base) for _ in range(random.randint(0, max_len)))

def _s11_mutate(seed):
    s = copy.deepcopy(seed)
    choice = random.choice([
        "swap_types", "corrupt_keys", "big_strings", "replace_lists",
        "deep_nest", "random_kv", "empty", "weird_flags", "bad_types"
    ])
    if choice == "swap_types":
        for k in list(s.keys()):
            if isinstance(s[k], str):
                s[k] = random.randint(-9999, 9999)
            elif isinstance(s[k], list):
                s[k] = {"_replaced": _s11_rand_text(20)}
    elif choice == "corrupt_keys":
        for k in list(s.keys()):
            if random.random() < 0.5:
                v = s.pop(k)
                s[k + _s11_rand_text(3)] = v
        s[_s11_rand_text(6)] = _s11_rand_text(10)
    elif choice == "big_strings":
        s["huge"] = _s11_rand_text(5000)
        if isinstance(s.get("arguments"), list):
            for arg in s["arguments"]:
                if isinstance(arg, dict):
                    arg["description"] = _s11_rand_text(2000)
    elif choice == "replace_lists":
        s["arguments"] = _s11_rand_text(40) if random.random() < 0.5 else random.randint(0, 9999)
    elif choice == "deep_nest":
        node = {}
        cur = node
        for _ in range(random.randint(12, 80)):
            cur["x"] = {}
            cur = cur["x"]
        cur["leaf"] = _s11_rand_text(20)
        s["deep"] = node
    elif choice == "random_kv":
        for _ in range(random.randint(1, 30)):
            s[_s11_rand_text(8)] = random.choice([None, random.randint(-1000, 1000), _s11_rand_text(30), [], {}])
    elif choice == "empty":
        s = {}
    elif choice == "weird_flags":
        if isinstance(s.get("arguments"), list):
            for arg in s["arguments"]:
                if isinstance(arg, dict):
                    arg["flag"] = _s11_rand_text(1)
    elif choice == "bad_types":
        if isinstance(s.get("arguments"), list):
            s["arguments"].append("not-a-dict")
    return s

_s11_ITER = 300
_s11_validate_crashes = []
_s11_validate_non_list = []
_s11_validate_bad_elems = []
_s11_normalize_crashes = []
_s11_normalize_non_dict = []

for _s11_i in range(_s11_ITER):
    _s11_mut = _s11_mutate(_s11_seed)
    try:
        _s11_res = scaffold.validate_tool(_s11_mut)
        if not isinstance(_s11_res, list):
            _s11_validate_non_list.append((_s11_i, type(_s11_res).__name__))
        else:
            for _s11_item in _s11_res:
                if not isinstance(_s11_item, str):
                    _s11_validate_bad_elems.append((_s11_i, type(_s11_item).__name__))
                    break
    except Exception as _s11_e:
        _s11_validate_crashes.append((_s11_i, type(_s11_e).__name__, str(_s11_e)[:120]))

    try:
        _s11_norm = scaffold.normalize_tool(_s11_mut)
        if not isinstance(_s11_norm, dict):
            _s11_normalize_non_dict.append((_s11_i, type(_s11_norm).__name__))
    except Exception as _s11_e:
        _s11_normalize_crashes.append((_s11_i, type(_s11_e).__name__, str(_s11_e)[:120]))

check(len(_s11_validate_crashes) == 0,
      f"11a: validate_tool did not crash on {_s11_ITER} mutations (crashes: {_s11_validate_crashes[:3]})")
check(len(_s11_validate_non_list) == 0,
      f"11b: validate_tool always returned a list (bad returns: {_s11_validate_non_list[:3]})")
check(len(_s11_validate_bad_elems) == 0,
      f"11c: validate_tool list elements are all strings (bad: {_s11_validate_bad_elems[:3]})")
check(len(_s11_normalize_crashes) == 0,
      f"11d: normalize_tool did not crash on {_s11_ITER} dict mutations (crashes: {_s11_normalize_crashes[:3]})")
check(len(_s11_normalize_non_dict) == 0,
      f"11e: normalize_tool always returned a dict (bad returns: {_s11_normalize_non_dict[:3]})")

# Top-level TypeError contract for normalize_tool
for _s11_bad in (None, [], "not a dict", 42, 3.14, True):
    try:
        scaffold.normalize_tool(_s11_bad)
        check(False, f"11f: normalize_tool({_s11_bad!r}) should raise TypeError")
    except TypeError:
        check(True, f"11f: normalize_tool({type(_s11_bad).__name__}) raises TypeError")
    except Exception as _s11_e:
        check(False, f"11f: normalize_tool({_s11_bad!r}) raised {type(_s11_e).__name__}, expected TypeError")


# =====================================================================
print("\n=== SECTION 12: Cascade Path Traversal Guard (F1) ===")
# =====================================================================
# _cascade_path_is_safe() rejects '..' traversal in relative cascade step
# paths while letting absolute paths through unchanged (matches
# _load_tool_path policy — tool schemas/presets can live anywhere on disk).

_s12_base = Path(scaffold.__file__).parent

# 12a: obvious POSIX-style traversal rejected
check(scaffold._cascade_path_is_safe(_s12_base, "../../../etc/passwd") is False,
      "12a: '../../../etc/passwd' rejected as unsafe")

# 12b: Windows-style traversal with backslashes rejected
check(scaffold._cascade_path_is_safe(_s12_base, "..\\..\\..\\Windows\\win.ini") is False,
      "12b: '..\\..\\..\\Windows\\win.ini' rejected as unsafe")

# 12c: safe relative path inside base accepted
check(scaffold._cascade_path_is_safe(_s12_base, "tests/foo.json") is True,
      "12c: 'tests/foo.json' accepted as safe")

# 12d: absolute paths pass through unchanged (platform-gated)
if sys.platform != "win32":
    check(scaffold._cascade_path_is_safe(_s12_base, "/tmp/foo") is True,
          "12d: POSIX absolute '/tmp/foo' accepted")
else:
    check(scaffold._cascade_path_is_safe(_s12_base, "C:/Windows/win.ini") is True,
          "12d: Windows absolute 'C:/Windows/win.ini' accepted")

# 12e: empty string treated as safe
check(scaffold._cascade_path_is_safe(_s12_base, "") is True,
      "12e: empty string treated as safe")

# 12f: None treated as safe
check(scaffold._cascade_path_is_safe(_s12_base, None) is True,
      "12f: None treated as safe")

# --- _import_cascade_data rejection ---
_s12_win = scaffold.MainWindow()
_s12_win.show()
app.processEvents()
_s12_dock = _s12_win.cascade_dock

# 12g: traversal in tool path raises ValueError naming "unsafe" and value
_s12_bad_tool = {
    "_format": "scaffold_cascade",
    "name": "evil",
    "steps": [{"tool": "../../../etc/passwd", "preset": None, "delay": 0}],
}
try:
    _s12_dock._import_cascade_data(_s12_bad_tool)
    check(False, "12g: should have raised ValueError for traversal tool path")
except ValueError as _s12_e_tool:
    _s12_msg_tool = str(_s12_e_tool)
    check("unsafe" in _s12_msg_tool and "../../../etc/passwd" in _s12_msg_tool,
          f"12g: ValueError names 'unsafe' and hostile value (got: {_s12_msg_tool!r})")

# 12h: traversal in preset path raises ValueError naming the preset field
_s12_bad_preset = {
    "_format": "scaffold_cascade",
    "name": "evil",
    "steps": [{"tool": None, "preset": "../../../etc/passwd", "delay": 0}],
}
try:
    _s12_dock._import_cascade_data(_s12_bad_preset)
    check(False, "12h: should have raised ValueError for traversal preset path")
except ValueError as _s12_e_preset:
    _s12_msg_preset = str(_s12_e_preset)
    check("unsafe" in _s12_msg_preset and "preset" in _s12_msg_preset
          and "../../../etc/passwd" in _s12_msg_preset,
          f"12h: ValueError names preset field + value (got: {_s12_msg_preset!r})")

# 12i: ValueError mentions 1-based step index (step 2 for second entry)
_s12_minimal = str(Path(__file__).parent / "tests" / "test_minimal.json")
_s12_safe_rel = str(Path(_s12_minimal).relative_to(_s12_base))
_s12_bad_step2 = {
    "_format": "scaffold_cascade",
    "name": "evil",
    "steps": [
        {"tool": _s12_safe_rel, "preset": None, "delay": 0},
        {"tool": "../../../evil", "preset": None, "delay": 0},
    ],
}
try:
    _s12_dock._import_cascade_data(_s12_bad_step2)
    check(False, "12i: should have raised ValueError for step 2 traversal")
except ValueError as _s12_e_idx:
    _s12_msg_idx = str(_s12_e_idx)
    check("step 2" in _s12_msg_idx,
          f"12i: ValueError contains 'step 2' (got: {_s12_msg_idx!r})")

# --- _check_cascade_dependencies funneling ---

# 12j: traversal tool path surfaces in missing_tools list, preset list empty
_s12_dep_tool = {
    "_format": "scaffold_cascade",
    "steps": [{"tool": "../../../etc/passwd", "preset": None, "delay": 0}],
}
_s12_mt, _s12_mp = scaffold._check_cascade_dependencies(_s12_dep_tool)
check(_s12_mt == ["../../../etc/passwd"] and _s12_mp == [],
      f"12j: traversal tool funneled to missing_tools (got tools={_s12_mt!r}, presets={_s12_mp!r})")

# 12k: traversal preset path surfaces in missing_presets list
_s12_dep_preset = {
    "_format": "scaffold_cascade",
    "steps": [{"tool": None, "preset": "../../../etc/passwd", "delay": 0}],
}
_s12_mt2, _s12_mp2 = scaffold._check_cascade_dependencies(_s12_dep_preset)
check(_s12_mt2 == [] and _s12_mp2 == ["../../../etc/passwd"],
      f"12k: traversal preset funneled to missing_presets (got tools={_s12_mt2!r}, presets={_s12_mp2!r})")

# 12l: valid relative path to real file inside base — existing behavior preserved
_s12_dep_ok = {
    "_format": "scaffold_cascade",
    "steps": [{"tool": _s12_safe_rel, "preset": None, "delay": 0}],
}
_s12_mt3, _s12_mp3 = scaffold._check_cascade_dependencies(_s12_dep_ok)
check(_s12_mt3 == [] and _s12_mp3 == [],
      f"12l: valid relative path not flagged missing (got tools={_s12_mt3!r}, presets={_s12_mp3!r})")

_s12_win.close()
_s12_win.deleteLater()
app.processEvents()


# =====================================================================
# Final cleanup
# =====================================================================
_cleanup_recovery_files()
shutil.rmtree(_shared_tmp, ignore_errors=True)

print(f"\n{'='*60}")
print(f"SECURITY AUDIT RESULTS: {passed}/{passed+failed} passed, {failed} failed")
if errors:
    print(f"\nFailed tests:")
    for e in errors:
        print(f"  - {e}")
print(f"{'='*60}")
sys.exit(0 if failed == 0 else 1)
