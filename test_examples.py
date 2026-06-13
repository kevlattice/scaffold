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

"""Test suite for the examples field — editable suggestions for string arguments."""

import json
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

app = QApplication.instance() or QApplication(sys.argv)

from scaffold import load_tool, validate_tool, normalize_tool, ToolForm, MainWindow

PASSED = 0
FAILED = 0

def check(condition, label, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS: {label}")
    else:
        FAILED += 1
        detail_str = f"\n        {detail}" if detail else ""
        print(f"  FAIL: {label}{detail_str}")


# --- Build a test schema with examples fields ---
def make_test_schema():
    return {
        "tool": "testtool",
        "binary": "testtool",
        "description": "Test tool for examples feature.",
        "subcommands": None,
        "arguments": [
            {
                "name": "Codec",
                "flag": "-c",
                "type": "string",
                "description": "Video codec to use",
                "examples": ["copy", "libx264", "libx265", "libvpx-vp9"],
                "required": False,
                "default": None,
            },
            {
                "name": "Codec With Default",
                "flag": "--codec-default",
                "type": "string",
                "description": "Codec with a default value",
                "examples": ["aac", "mp3", "opus", "flac"],
                "required": False,
                "default": "aac",
            },
            {
                "name": "Plain String",
                "flag": "--name",
                "type": "string",
                "description": "A plain string with no examples",
                "required": False,
                "default": None,
            },
            {
                "name": "Format",
                "flag": "-f",
                "type": "string",
                "description": "Output format",
                "examples": ["mp4", "mkv", "avi", "webm"],
                "required": False,
                "default": None,
                "validation": "^[a-z0-9]+$",
            },
            {
                "name": "Enable Feature",
                "flag": "--enable",
                "type": "boolean",
                "description": "Enable advanced features",
                "required": False,
                "default": None,
            },
            {
                "name": "Advanced Option",
                "flag": "--advanced",
                "type": "string",
                "description": "Advanced option that depends on --enable",
                "examples": ["fast", "slow", "balanced"],
                "required": False,
                "default": None,
                "depends_on": "--enable",
            },
            {
                "name": "Target",
                "flag": "TARGET",
                "type": "string",
                "description": "Input file",
                "required": True,
                "default": None,
                "positional": True,
            },
        ],
    }


def build_form(schema=None):
    data = schema or make_test_schema()
    errors = validate_tool(data)
    assert not errors, f"Schema validation failed: {errors}"
    data = normalize_tool(data)
    return ToolForm(data)


# =====================================================================
print("=== TEST 1: Widget type selection ===")
form = build_form()

# Codec has examples -> should be QComboBox
codec_w = form.fields[("__global__", "-c")]["widget"]
check(isinstance(codec_w, QComboBox), "examples field renders as QComboBox")
check(codec_w.isEditable(), "QComboBox is editable")

# Plain String has no examples -> should be QLineEdit
plain_w = form.fields[("__global__", "--name")]["widget"]
check(isinstance(plain_w, QLineEdit), "no-examples field renders as QLineEdit")

# =====================================================================
print("\n=== TEST 2: Examples populated in dropdown ===")
# Should have empty item + 4 examples = 5 items
check(codec_w.count() == 5, f"combo has 5 items (empty + 4 examples)",
      f"got {codec_w.count()}")
check(codec_w.itemText(0) == "", "first item is empty")
check(codec_w.itemText(1) == "copy", f"second item is 'copy', got '{codec_w.itemText(1)}'")
check(codec_w.itemText(2) == "libx264", f"third item is 'libx264'")
check(codec_w.itemText(3) == "libx265", f"fourth item is 'libx265'")
check(codec_w.itemText(4) == "libvpx-vp9", f"fifth item is 'libvpx-vp9'")

# =====================================================================
print("\n=== TEST 3: Default value ===")
# Codec has no default -> current text should be empty
check(codec_w.currentText() == "", "no default: current text is empty",
      f"got '{codec_w.currentText()}'")

# Codec With Default has default "aac"
default_w = form.fields[("__global__", "--codec-default")]["widget"]
check(isinstance(default_w, QComboBox), "default examples field is QComboBox")
check(default_w.currentText() == "aac", "default value 'aac' is set",
      f"got '{default_w.currentText()}'")

# =====================================================================
print("\n=== TEST 4: Selecting an example value ===")
form.fields[("__global__", "TARGET")]["widget"].setText("input.mp4")

codec_w.setCurrentText("libx264")
val = form.get_field_value(("__global__", "-c"))
check(val == "libx264", "selecting example returns correct value",
      f"got '{val}'")

cmd, display = form.build_command()
check("-c" in cmd, "-c flag in command")
idx = cmd.index("-c")
check(cmd[idx + 1] == "libx264", "libx264 follows -c in command",
      f"got '{cmd[idx + 1]}'")

# =====================================================================
print("\n=== TEST 5: Typing a custom value ===")
codec_w.setCurrentText("hevc_nvenc")
val = form.get_field_value(("__global__", "-c"))
check(val == "hevc_nvenc", "custom typed value returned correctly",
      f"got '{val}'")

cmd, display = form.build_command()
idx = cmd.index("-c")
check(cmd[idx + 1] == "hevc_nvenc", "custom value in command list",
      f"got '{cmd[idx + 1]}'")

# =====================================================================
print("\n=== TEST 6: Empty value excluded from command ===")
codec_w.setCurrentText("")
val = form.get_field_value(("__global__", "-c"))
check(val is None, "empty combo returns None")

cmd, _ = form.build_command()
check("-c" not in cmd, "-c not in command when empty",
      f"cmd={cmd}")

# =====================================================================
print("\n=== TEST 7: Whitespace-only value excluded ===")
codec_w.setCurrentText("   ")
val = form.get_field_value(("__global__", "-c"))
check(val is None, "whitespace-only returns None")

# =====================================================================
print("\n=== TEST 8: Placeholder text ===")
check(codec_w.lineEdit().placeholderText() == "Video codec to use",
      "placeholder text from description",
      f"got '{codec_w.lineEdit().placeholderText()}'")

# =====================================================================
print("\n=== TEST 9: Preset round-trip ===")
codec_w.setCurrentText("libx265")
default_w.setCurrentText("opus")
form.fields[("__global__", "TARGET")]["widget"].setText("test.mp4")

# Serialize
preset = form.serialize_values()
check(preset.get("-c") == "libx265", "preset has -c = libx265",
      f"got {preset.get('-c')}")
check(preset.get("--codec-default") == "opus", "preset has --codec-default = opus",
      f"got {preset.get('--codec-default')}")

# Reset
form.reset_to_defaults()
check(codec_w.currentText() == "", "after reset: codec is empty")
check(default_w.currentText() == "aac", "after reset: codec-default back to 'aac'",
      f"got '{default_w.currentText()}'")

# Apply preset
form.apply_values(preset)
check(codec_w.currentText() == "libx265", "after apply: codec is libx265",
      f"got '{codec_w.currentText()}'")
check(default_w.currentText() == "opus", "after apply: codec-default is opus",
      f"got '{default_w.currentText()}'")

# =====================================================================
print("\n=== TEST 10: Preset with custom (non-example) value ===")
codec_w.setCurrentText("totally_custom_codec")
preset2 = form.serialize_values()
check(preset2.get("-c") == "totally_custom_codec", "custom value serialized")

form.reset_to_defaults()
form.apply_values(preset2)
check(codec_w.currentText() == "totally_custom_codec",
      "custom value restored from preset",
      f"got '{codec_w.currentText()}'")

# =====================================================================
print("\n=== TEST 11: Validation on examples field ===")
format_w = form.fields[("__global__", "-f")]["widget"]
check(isinstance(format_w, QComboBox), "format field with validation is QComboBox")
check(format_w.isEditable(), "format field is editable")

# Valid value
format_w.setCurrentText("mp4")
style = format_w.lineEdit().styleSheet()
check("red" not in style, "valid value: no red border",
      f"style='{style}'")

# Invalid value
format_w.setCurrentText("MP4!!!")
style = format_w.lineEdit().styleSheet()
check("red" in style, "invalid value: red border shown",
      f"style='{style}'")

# Clear -> no red
format_w.setCurrentText("")
style = format_w.lineEdit().styleSheet()
check("red" not in style, "empty value: no red border")

# =====================================================================
print("\n=== TEST 12: Dependency with examples field ===")
enable_w = form.fields[("__global__", "--enable")]["widget"]
advanced_w = form.fields[("__global__", "--advanced")]["widget"]

check(isinstance(advanced_w, QComboBox), "dependent examples field is QComboBox")
check(not advanced_w.isEnabled(), "dependent field disabled when parent unchecked")

# Enable parent
enable_w.setChecked(True)
check(advanced_w.isEnabled(), "dependent field enabled when parent checked")

# Set value and verify in command
advanced_w.setCurrentText("fast")
cmd, _ = form.build_command()
check("--advanced" in cmd, "--advanced in command when enabled")
idx = cmd.index("--advanced")
check(cmd[idx + 1] == "fast", "value 'fast' follows --advanced")

# Disable parent -> value excluded
enable_w.setChecked(False)
val = form.get_field_value(("__global__", "--advanced"))
check(val is None, "dependent field returns None when parent disabled")
cmd, _ = form.build_command()
check("--advanced" not in cmd, "--advanced excluded when parent disabled")

# Re-enable -> value preserved
enable_w.setChecked(True)
check(advanced_w.currentText() == "fast", "value preserved after re-enable",
      f"got '{advanced_w.currentText()}'")

# =====================================================================
print("\n=== TEST 13: _raw_field_value ignores enabled state ===")
enable_w.setChecked(False)
check(not advanced_w.isEnabled(), "advanced is disabled")
raw = form._raw_field_value(("__global__", "--advanced"))
check(raw == "fast", "_raw_field_value reads 'fast' even when disabled",
      f"got '{raw}'")

# =====================================================================
print("\n=== TEST 14: Validation rejects examples on enum type ===")
bad_schema = {
    "tool": "bad",
    "binary": "bad",
    "description": "test",
    "arguments": [
        {
            "name": "Mode",
            "flag": "--mode",
            "type": "enum",
            "choices": ["a", "b"],
            "examples": ["x", "y"],
        }
    ],
}
errors = validate_tool(bad_schema)
has_warning = any("choices" in e and "examples" in e for e in errors)
check(has_warning, "validator warns about enum + examples",
      f"errors={errors}")

# =====================================================================
print("\n=== TEST 15: Validation rejects non-string-list examples ===")
bad_schema2 = {
    "tool": "bad2",
    "binary": "bad2",
    "description": "test",
    "arguments": [
        {
            "name": "Thing",
            "flag": "--thing",
            "type": "string",
            "examples": [1, 2, 3],
        }
    ],
}
errors2 = validate_tool(bad_schema2)
has_type_error = any("list of strings" in e for e in errors2)
check(has_type_error, "validator rejects non-string examples",
      f"errors={errors2}")

# =====================================================================
print("\n=== TEST 16: Schema with no examples field normalizes to null ===")
minimal = {
    "tool": "minimal",
    "binary": "minimal",
    "description": "test",
    "arguments": [
        {"name": "Foo", "flag": "--foo", "type": "string"},
    ],
}
minimal = normalize_tool(minimal)
check(minimal["arguments"][0].get("examples") is None,
      "missing examples normalizes to None")

# =====================================================================
print("\n=== TEST 17: Full command assembly with mixed fields ===")
form2 = build_form()
form2.fields[("__global__", "-c")]["widget"].setCurrentText("libx264")
form2.fields[("__global__", "--codec-default")]["widget"].setCurrentText("mp3")
form2.fields[("__global__", "--name")]["widget"].setText("myproject")
form2.fields[("__global__", "TARGET")]["widget"].setText("input.mkv")

cmd, display = form2.build_command()
check(cmd[0] == "testtool", "binary is first")
check("input.mkv" == cmd[-1], "positional is last",
      f"last={cmd[-1]}")
check("-c" in cmd and cmd[cmd.index("-c") + 1] == "libx264",
      "-c libx264 in command")
check("--codec-default" in cmd and cmd[cmd.index("--codec-default") + 1] == "mp3",
      "--codec-default mp3 in command")
check("--name" in cmd and cmd[cmd.index("--name") + 1] == "myproject",
      "--name myproject in command")


# =====================================================================
print(f"\n{'=' * 60}")
print(f"EXAMPLES FEATURE RESULTS: {PASSED}/{PASSED + FAILED} passed, {FAILED} failed")
if FAILED == 0:
    print("ALL TESTS PASSED")
print(f"{'=' * 60}")
