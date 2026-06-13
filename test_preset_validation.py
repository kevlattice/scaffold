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

"""Tests for validate_preset() — Phase 1 of Import Validation."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

import scaffold

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
print("\n=== Preset Validation: Basic Structure ===")
# =====================================================================

# 1. Valid preset dict passes
result = scaffold.validate_preset({"__global__:--verbose": True, "__global__:--output": "/tmp/out"})
check(result == [], f"1: valid preset passes (got {result})")

# 2. Empty dict passes (user saved with all defaults)
result = scaffold.validate_preset({})
check(result == [], f"2: empty dict passes (got {result})")

# 3. Non-dict: list
result = scaffold.validate_preset([1, 2, 3])
check(len(result) > 0 and "dict" in result[0].lower(), f"3: list rejected (got {result})")

# 4. Non-dict: string
result = scaffold.validate_preset("not a dict")
check(len(result) > 0 and "dict" in result[0].lower(), f"4: string rejected (got {result})")

# 5. Non-dict: None
result = scaffold.validate_preset(None)
check(len(result) > 0, f"5: None rejected (got {result})")

# 6. Non-dict: integer
result = scaffold.validate_preset(42)
check(len(result) > 0, f"6: integer rejected (got {result})")


# =====================================================================
print("\n=== Preset Validation: Key Checks ===")
# =====================================================================

# 7. Dict with non-string key fails
result = scaffold.validate_preset({123: "value"})
check(any("key must be a string" in e.lower() for e in result), f"7: non-string key rejected (got {result})")

# 8. Keys starting with _ are allowed
result = scaffold.validate_preset({"_schema_hash": "abc12345", "_subcommand": "push"})
check(result == [], f"8: underscore meta keys pass (got {result})")

# 9. Absurdly long key fails
long_key = "x" * 10_001
result = scaffold.validate_preset({long_key: "val"})
check(any("too long" in e.lower() for e in result), f"9: long key rejected (got {result})")


# =====================================================================
print("\n=== Preset Validation: Value Type Checks ===")
# =====================================================================

# 10. Nested dict value fails
result = scaffold.validate_preset({"key": {"nested": "dict"}})
check(any("unsupported type" in e.lower() for e in result), f"10: nested dict rejected (got {result})")

# 11. List of strings passes (multi_enum)
result = scaffold.validate_preset({"__global__:--tags": ["a", "b", "c"]})
check(result == [], f"11: list of strings passes (got {result})")

# 12. List of ints fails
result = scaffold.validate_preset({"key": [1, 2, 3]})
check(any("must be a string" in e.lower() for e in result), f"12: list of ints rejected (got {result})")

# 13. List with mixed types fails
result = scaffold.validate_preset({"key": ["ok", 42]})
check(any("must be a string" in e.lower() for e in result), f"13: mixed list rejected (got {result})")

# 14. Absurdly long string value fails
long_val = "x" * 10_001
result = scaffold.validate_preset({"key": long_val})
check(any("too long" in e.lower() for e in result), f"14: long value rejected (got {result})")

# 15. None value is allowed
result = scaffold.validate_preset({"key": None})
check(result == [], f"15: None value passes (got {result})")

# 16. Int value is allowed
result = scaffold.validate_preset({"key": 42})
check(result == [], f"16: int value passes (got {result})")

# 17. Float value is allowed
result = scaffold.validate_preset({"key": 3.14})
check(result == [], f"17: float value passes (got {result})")

# 18. Bool value is allowed
result = scaffold.validate_preset({"key": True})
check(result == [], f"18: bool value passes (got {result})")


# =====================================================================
print("\n=== Preset Validation: Schema-as-Preset Detection ===")
# =====================================================================

# 19. Dict that looks like a schema warns
result = scaffold.validate_preset({"binary": "nmap", "arguments": [{"name": "x"}]})
check(any("tool schema" in e.lower() for e in result), f"19: schema-as-preset detected (got {result})")

# 20. Dict with only "binary" is fine (could be a valid preset key)
result = scaffold.validate_preset({"binary": "nmap"})
check(not any("tool schema" in e.lower() for e in result), f"20: only-binary key is fine (got {result})")


# =====================================================================
print("\n=== Preset Validation: tool_data Cross-Check ===")
# =====================================================================

tool_data = {
    "tool": "test",
    "binary": "test",
    "description": "test",
    "arguments": [
        {"name": "verbose", "flag": "--verbose", "type": "boolean"},
        {"name": "output", "flag": "--output", "type": "string"},
    ],
}

# 21. Valid keys matching schema pass
# Global flags are stored flat (no "__global__:" prefix) — this matches
# what serialize_values writes and what apply_values reads.
result = scaffold.validate_preset(
    {"--verbose": True, "--output": "/tmp"},
    tool_data=tool_data,
)
check(result == [], f"21: matching keys pass with tool_data (got {result})")

# 22. Unknown keys produce warning
result = scaffold.validate_preset(
    {"--verbose": True, "--nonexistent": "x"},
    tool_data=tool_data,
)
check(any("unknown" in e.lower() for e in result), f"22: unknown key warned (got {result})")

# 23. Meta keys are not flagged as unknown
result = scaffold.validate_preset(
    {"_schema_hash": "abc12345", "--verbose": True},
    tool_data=tool_data,
)
check(result == [], f"23: meta keys not flagged as unknown (got {result})")


# =====================================================================
print("\n=== Preset Validation: F6 key-format round-trip ===")
# =====================================================================

# Schema with BOTH a global flag and a subcommand flag. Round-tripping
# through serialize_values -> validate_preset(tool_data=) must produce
# zero errors. Before F6 was fixed, global flags came back flat but
# known_flags was built with "__global__:" prefix, so every global-flag
# preset failed the cross-check.
_f6_schema = {
    "tool": "roundtrip_tool",
    "binary": "roundtrip",
    "description": "F6 round-trip schema",
    "arguments": [
        {"name": "Verbose", "flag": "--verbose", "type": "boolean"},
        {"name": "Output", "flag": "--output", "type": "string"},
    ],
    "subcommands": [
        {
            "name": "scan",
            "description": "scan subcommand",
            "arguments": [
                {"name": "Timeout", "flag": "--timeout", "type": "integer", "min": 0, "max": 600},
            ],
        },
    ],
}
_f6_errors = scaffold.validate_tool(_f6_schema)
assert not _f6_errors, f"test schema invalid: {_f6_errors}"
_f6_data = scaffold.normalize_tool(_f6_schema)
_f6_form = scaffold.ToolForm(_f6_data)

# Pick the scan subcommand so the scan:--timeout field is live.
_f6_form.sub_combo.setCurrentIndex(_f6_form.sub_combo.findData("scan"))

# Set a global-scope value and a subcommand-scope value.
_f6_form._set_field_value((scaffold.ToolForm.GLOBAL, "--verbose"), True)
_f6_form._set_field_value((scaffold.ToolForm.GLOBAL, "--output"), "/tmp/out")
_f6_form._set_field_value(("scan", "--timeout"), 30)

_f6_preset = _f6_form.serialize_values()

# 24. Round-trip — the core regression guard
result = scaffold.validate_preset(_f6_preset, tool_data=_f6_data)
check(result == [], f"24: serialize_values -> validate_preset(tool_data=) round-trips cleanly (got {result})")

# 25. Serialized global keys are flat (no "__global__:" prefix) — this
# documents the wire format that validate_preset must accept.
check(
    "--verbose" in _f6_preset and "__global__:--verbose" not in _f6_preset,
    f"25: global flag is stored flat in the preset (keys: {sorted(k for k in _f6_preset if not k.startswith('_'))})",
)

# 26. Serialized subcommand keys are "scope:flag"-prefixed
check(
    "scan:--timeout" in _f6_preset,
    f"26: subcommand flag is stored scope-prefixed (keys: {sorted(k for k in _f6_preset if not k.startswith('_'))})",
)

# 27. Unknown-key machinery still fires for truly stale flags — the fix
# only changed the format, not the presence of the check.
result = scaffold.validate_preset(
    {"--this-flag-does-not-exist": True},
    tool_data=_f6_data,
)
check(
    any("unknown preset key" in e.lower() for e in result),
    f"27: stale flag still flagged as Unknown preset key (got {result})",
)

# 28. Subcommand scope unchanged — a valid scope:flag key still validates
# cleanly. Guards against an accidental regression on line 659.
result = scaffold.validate_preset({"scan:--timeout": 30}, tool_data=_f6_data)
check(result == [], f"28: subcommand scope:flag key validates cleanly (got {result})")

# 29. Meta keys still skipped — _format, _tool, _subcommand, _schema_hash
# must not trigger unknown-key errors. Guards the meta-key bypass.
result = scaffold.validate_preset(
    {
        "_format": "scaffold_preset",
        "_tool": "roundtrip_tool",
        "_subcommand": "scan",
        "_schema_hash": "abc12345",
        "--verbose": True,
    },
    tool_data=_f6_data,
)
check(result == [], f"29: meta keys not treated as unknown (got {result})")


# =====================================================================
print("\n=== Preset Validation: F6 shipped-preset cohort ===")
# =====================================================================

# Walk every default preset under scaffold_data/default_presets/<toolname>/,
# pair with scaffold_data/tools/<toolname>.json, and assert
# validate_preset(tool_data=) yields zero errors. Pass 2 Check 4 recorded
# 53/53 failing before F6 landed. (v2.12.0 Phase 3 moved bundled files
# into scaffold_data/.)
_presets_root = Path(__file__).parent / "scaffold_data" / "default_presets"
_tools_root = Path(__file__).parent / "scaffold_data" / "tools"

_cohort_total = 0
_cohort_failures = []
_cohort_skipped = []
_cohort_schema_cache = {}

for _preset_path in sorted(_presets_root.glob("*/*.json")):
    _cohort_total += 1
    _toolname = _preset_path.parent.name
    _rel = _preset_path.relative_to(_presets_root).as_posix()

    if _toolname not in _cohort_schema_cache:
        _schema_path = _tools_root / f"{_toolname}.json"
        if not _schema_path.exists():
            _cohort_schema_cache[_toolname] = (None, f"no schema at scaffold_data/tools/{_toolname}.json")
        else:
            try:
                _raw = scaffold.load_tool(_schema_path)
                _tool_errors = scaffold.validate_tool(_raw)
                if _tool_errors:
                    _cohort_schema_cache[_toolname] = (None, f"validate_tool failed: {_tool_errors[0]}")
                else:
                    _cohort_schema_cache[_toolname] = (scaffold.normalize_tool(_raw), None)
            except Exception as e:
                _cohort_schema_cache[_toolname] = (None, f"load_tool raised: {e}")

    _schema, _skip_reason = _cohort_schema_cache[_toolname]
    if _schema is None:
        _cohort_skipped.append((_rel, _skip_reason))
        continue

    try:
        _preset_data = json.loads(_preset_path.read_text(encoding="utf-8"))
    except Exception as e:
        _cohort_skipped.append((_rel, f"json parse: {e}"))
        continue

    _errs = scaffold.validate_preset(_preset_data, tool_data=_schema)
    if _errs:
        _cohort_failures.append((_rel, _errs))

# 30. All shipped presets validate cleanly against their tool schema
check(
    _cohort_failures == [],
    f"30: all {_cohort_total} shipped default presets validate cleanly (failures: {_cohort_failures[:3]})",
)

# 31. We actually walked the expected cohort — if this count drifts, the
# test is no longer exercising what the prompt claimed (53 shipped pairs).
check(
    _cohort_total >= 50,
    f"31: shipped-preset cohort size sanity (walked {_cohort_total}, skipped {len(_cohort_skipped)})",
)

if _cohort_skipped:
    print(f"  note: {len(_cohort_skipped)} preset(s) skipped:")
    for _name, _reason in _cohort_skipped:
        print(f"    - {_name}: {_reason}")


# =====================================================================
print("\n=== Preset Validation: F12 — UI paths have parallel _format handling ===")
# =====================================================================
# Static-scan regression guard. F12 unified the missing-_format policy so
# both interactive UI paths now PROMPT on missing marker and REJECT on
# wrong marker. A future refactor that silently drops either branch on
# _on_load_preset (the path that used to silently accept missing _format)
# would reintroduce the hole. The prompts themselves go through
# QMessageBox so unit-testing behavior directly is brittle; asserting the
# source text contains the prompt + critical calls is a cheap guard that
# fires on any accidental removal.
import re as _re12

_scaffold_src = Path(__file__).parent / "scaffold.py"
_scaffold_text = _scaffold_src.read_text(encoding="utf-8")


def _f12_extract_method(src, name):
    """Return the textual body of ``def NAME(`` through the next sibling def."""
    lines = src.splitlines(keepends=True)
    pat = _re12.compile(rf"^(\s*)def\s+{_re12.escape(name)}\s*\(")
    start = None
    indent = ""
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            start = i
            indent = m.group(1)
            break
    if start is None:
        return ""
    sibling = _re12.compile(rf"^{_re12.escape(indent)}(def|class)\s")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() == "":
            continue
        if sibling.match(line):
            end = j
            break
        stripped_indent = len(line) - len(line.lstrip())
        if line.strip() and stripped_indent < len(indent):
            end = j
            break
    return "".join(lines[start:end])


# v2.12.1 extracted the validation pipeline into _apply_preset_from_path so
# both _on_load_preset (picker path) and dropEvent route through one helper.
# Concatenate the picker entry and the helper bodies so this static-scan
# guard remains agnostic to where the format checks physically live.
_load_body = (
    _f12_extract_method(_scaffold_text, "_on_load_preset")
    + _f12_extract_method(_scaffold_text, "_apply_preset_from_path")
)
_import_body = _f12_extract_method(_scaffold_text, "_on_import_preset")

# 32. UI preset-load path prompts on missing _format (not silent anymore)
check(
    "Missing Format Marker" in _load_body and "Load anyway" in _load_body,
    "32: preset-load path contains Missing Format Marker prompt with 'Load anyway'",
)

# 33. UI preset-load path still rejects wrong _format via QMessageBox.critical
check(
    "Wrong File Format" in _load_body and "QMessageBox.critical" in _load_body,
    "33: preset-load path contains Wrong File Format critical dialog",
)

# 34. Consistency — _on_import_preset's parallel behavior is still present
check(
    "Missing Format Marker" in _import_body and "Import anyway" in _import_body,
    "34: _on_import_preset contains Missing Format Marker prompt with 'Import anyway'",
)

# 35. _on_import_preset still rejects wrong _format
check(
    "Wrong File Format" in _import_body and "QMessageBox.critical" in _import_body,
    "35: _on_import_preset contains Wrong File Format critical dialog",
)


# =====================================================================
print("\n=== Preset Validation: F16 — cascade paths call validate_preset ===")
# =====================================================================
# Static-scan regression guard. F16 added validate_preset to both cascade
# paths so malformed presets can't silently feed apply_values during
# unattended runs. v2.11.4 wired tool_data= and report_unknown_keys=False
# at these sites so per-key type rules fire too. A future refactor that
# drops either piece reopens a hole the audit flagged.

_slot_body = _f12_extract_method(_scaffold_text, "_on_slot_clicked")
_chain_body = _f12_extract_method(_scaffold_text, "_chain_advance")

# 36. _on_slot_clicked calls validate_preset with preset_data + tool_data
check(
    "validate_preset(" in _slot_body
    and "preset_data," in _slot_body
    and "tool_data=" in _slot_body
    and "report_unknown_keys=False" in _slot_body,
    "36: _on_slot_clicked calls validate_preset with tool_data + report_unknown_keys=False",
)

# 37. _chain_advance calls validate_preset with preset_data + tool_data
check(
    "validate_preset(" in _chain_body
    and "preset_data," in _chain_body
    and "tool_data=" in _chain_body
    and "report_unknown_keys=False" in _chain_body,
    "37: _chain_advance calls validate_preset with tool_data + report_unknown_keys=False",
)


# =====================================================================
print("\n=== Preset Validation: F16 — structural-error detection still catches malformed presets ===")
# =====================================================================
# Behavior regression guard for the structural checks the new cascade
# calls rely on. If validate_preset stops flagging any of these cases,
# the new cascade gate silently degrades — the gate still runs but no
# longer rejects the thing it was added to catch.

# 38. Nested dict value is rejected (unsupported value type)
_f16_err_nested = scaffold.validate_preset({"--flag": {"nested": "dict"}})
check(
    len(_f16_err_nested) > 0 and any("unsupported type" in e.lower() for e in _f16_err_nested),
    f"38: validate_preset flags nested dict value (got {_f16_err_nested})",
)

# 39. Schema-as-preset mistake is rejected
_f16_err_schema = scaffold.validate_preset({"binary": "nmap", "arguments": [{"name": "x"}]})
check(
    len(_f16_err_schema) > 0 and any("tool schema" in e.lower() for e in _f16_err_schema),
    f"39: validate_preset flags schema-as-preset (got {_f16_err_schema})",
)

# 40. Oversized key is rejected
_f16_long_key = "x" * 10_001
_f16_err_longkey = scaffold.validate_preset({_f16_long_key: "val"})
check(
    len(_f16_err_longkey) > 0 and any("too long" in e.lower() for e in _f16_err_longkey),
    f"40: validate_preset flags oversized key (got {_f16_err_longkey})",
)


# =====================================================================
print("\n=== Preset Validation: F13 — UI preset size-gate uses MAX_PRESET_SIZE ===")
# =====================================================================
# Static-scan regression guard. F13 switched both UI preset-load paths
# from the schema-specific MAX_SCHEMA_SIZE to the preset-specific
# MAX_PRESET_SIZE constant. A future refactor that swaps either one
# back would reintroduce the 1 MB vs 2 MB asymmetry the audit caught
# (cascade accepts a preset the UI rejects, same file).

# 41. UI preset-load path no longer references MAX_SCHEMA_SIZE
check(
    "MAX_SCHEMA_SIZE" not in _load_body,
    "41: preset-load path does not reference MAX_SCHEMA_SIZE",
)

# 42. _on_import_preset body no longer references MAX_SCHEMA_SIZE
check(
    "MAX_SCHEMA_SIZE" not in _import_body,
    "42: _on_import_preset body does not reference MAX_SCHEMA_SIZE",
)

# 43. UI preset-load path uses MAX_PRESET_SIZE
check(
    "MAX_PRESET_SIZE" in _load_body,
    "43: preset-load path references MAX_PRESET_SIZE",
)

# 44. _on_import_preset body uses MAX_PRESET_SIZE
check(
    "MAX_PRESET_SIZE" in _import_body,
    "44: _on_import_preset body references MAX_PRESET_SIZE",
)


# =====================================================================
print("\n=== Preset Validation: F13 — preset/schema size-cap constants ===")
# =====================================================================
# Constant-existence + ordering regression guard. The F13 fix assumes
# MAX_PRESET_SIZE >= MAX_SCHEMA_SIZE (otherwise migrating UI paths to
# the preset constant would tighten limits, which wasn't the intent).
# Inverting the ordering later would silently reintroduce an asymmetry.

# 45. MAX_SCHEMA_SIZE exists and is an int
check(
    hasattr(scaffold, "MAX_SCHEMA_SIZE") and isinstance(scaffold.MAX_SCHEMA_SIZE, int),
    "45: scaffold.MAX_SCHEMA_SIZE exists and is an int",
)

# 46. MAX_PRESET_SIZE exists and is an int
check(
    hasattr(scaffold, "MAX_PRESET_SIZE") and isinstance(scaffold.MAX_PRESET_SIZE, int),
    "46: scaffold.MAX_PRESET_SIZE exists and is an int",
)

# 47. MAX_PRESET_SIZE >= MAX_SCHEMA_SIZE (preset cap never tighter than schema cap)
check(
    scaffold.MAX_PRESET_SIZE >= scaffold.MAX_SCHEMA_SIZE,
    f"47: MAX_PRESET_SIZE ({scaffold.MAX_PRESET_SIZE}) >= "
    f"MAX_SCHEMA_SIZE ({scaffold.MAX_SCHEMA_SIZE})",
)


# =====================================================================
print("\n=== Preset Validation: F8 — PRESET_META_KEY_TYPES registry ===")
# =====================================================================
# Regression guard for F8: meta-key values are now type-checked against a
# module-level registry. None is treated as missing (preserves the
# missing-marker rejection paths at the UI layer). Unknown _foo keys pass
# with a stderr debug line (forward-compat with future meta-keys).

# 48–54. Each registered key must reject a wrong-type value with an error
# containing "wrong type". One assertion per key.
_wrong_type_cases = [
    ("_format", 123),
    ("_tool", 123),
    ("_subcommand", 123),
    ("_schema_hash", 123),
    ("_elevated", 1),        # int, not bool — isinstance(1, (bool,)) is False
    ("_extra_flags", 123),
    ("_description", 123),
]
_wt_num = 48
for _key, _bad_val in _wrong_type_cases:
    _res = scaffold.validate_preset({_key: _bad_val})
    check(
        any("wrong type" in e for e in _res),
        f"{_wt_num}: wrong-type {_key}={_bad_val!r} produces 'wrong type' error (got {_res})",
    )
    _wt_num += 1

# 55–61. Each registered key must accept None with zero errors
# (None-as-missing). One assertion per key.
_none_num = 55
for _key in ("_format", "_tool", "_subcommand",
             "_schema_hash", "_elevated", "_extra_flags", "_description"):
    _res = scaffold.validate_preset({_key: None})
    check(
        _res == [],
        f"{_none_num}: {_key}=None is treated as missing (got {_res})",
    )
    _none_num += 1

# 62. _elevated: True passes
_res = scaffold.validate_preset({"_elevated": True})
check(_res == [], f"62: _elevated=True passes (got {_res})")

# 63. _elevated: False passes
_res = scaffold.validate_preset({"_elevated": False})
check(_res == [], f"63: _elevated=False passes (got {_res})")

# 64. Unknown meta-key passes for forward-compat. Suppress stderr so the
# debug line doesn't pollute test output — we only care about the return.
import io
import contextlib
_buf = io.StringIO()
with contextlib.redirect_stderr(_buf):
    _res = scaffold.validate_preset({"_mystery_key": "x"})
check(
    _res == [],
    f"64: unknown meta-key _mystery_key passes for forward-compat (got {_res})",
)

# 65. Baseline: a well-formed preset emitted by serialize_values() on a
# real schema still passes validate_preset with zero errors. Guards
# against the registry accidentally breaking the round-trip (e.g., if
# serialize_values writes a meta-key type that doesn't match the
# registry). Reuses the F6 schema + form built above.
_f8_preset = _f6_form.serialize_values()
_res = scaffold.validate_preset(_f8_preset, tool_data=_f6_data)
check(
    _res == [],
    f"65: serialize_values() round-trip still clean with registry (got {_res})",
)


# =====================================================================
# Final results
# =====================================================================
print(f"\n{'='*60}")
print(f"PRESET VALIDATION TEST RESULTS: {passed}/{passed+failed} passed, {failed} failed")
if errors:
    print(f"\nFailed tests:")
    for e in errors:
        print(f"  - {e}")
print(f"{'='*60}")

sys.exit(0 if failed == 0 else 1)
