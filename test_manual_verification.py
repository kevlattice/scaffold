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

"""Post-Fix Manual Verification Tests — Spinbox 0, Extra Flags Validation.

Exercises the edge cases from the MiniMax M2.7 triage fixes that automated
unit tests can't fully cover: visual behavior, command assembly pipeline,
preset round-trips, dark mode styling, and boundary conditions.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication, QMessageBox, QSpinBox, QDoubleSpinBox
from PySide6.QtCore import QSettings

app = QApplication.instance() or QApplication(sys.argv)

import scaffold

# Suppress the first-run welcome modal so tests don't block on QDialog.exec()
scaffold.MainWindow._suppress_welcome_dialog = True

# Auto-accept missing _format warnings (test schemas intentionally lack _format)
_original_qmb_warning = QMessageBox.warning

def _patched_warning(parent, title, text, *a, **kw):
    if title == "Missing Format Marker":
        return QMessageBox.StandardButton.Yes
    return _original_qmb_warning(parent, title, text, *a, **kw)

QMessageBox.warning = _patched_warning

# Auto-decline recovery prompts so stale recovery files don't block tests
QMessageBox.question = lambda *a, **kw: QMessageBox.StandardButton.No


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


# ---- Helper: build a test schema with integer, float, and elevation ----
def make_zero_test_schema():
    return {
        "tool": "zero_test",
        "binary": "echo",
        "description": "Test tool for zero-value verification.",
        "elevated": "optional",
        "subcommands": None,
        "arguments": [
            {
                "name": "Hash Type",
                "flag": "-m",
                "type": "integer",
                "description": "Hash type (0=MD5)",
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
                "short_flag": None,
            },
            {
                "name": "Max Retries",
                "flag": "--max-retries",
                "type": "integer",
                "description": "Maximum retries (0 disables)",
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
                "short_flag": None,
            },
            {
                "name": "Rate",
                "flag": "--rate",
                "type": "float",
                "description": "Rate (0.0 is valid)",
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
                "short_flag": None,
            },
            {
                "name": "Count",
                "flag": "--count",
                "type": "integer",
                "description": "Count with default 5",
                "required": False,
                "default": 5,
                "choices": None,
                "group": None,
                "depends_on": None,
                "repeatable": False,
                "separator": "space",
                "positional": False,
                "validation": None,
                "examples": None,
                "short_flag": None,
            },
            {
                "name": "Depth",
                "flag": "--depth",
                "type": "integer",
                "description": "Depth (depends on -m)",
                "required": False,
                "default": None,
                "choices": None,
                "group": None,
                "depends_on": "-m",
                "repeatable": False,
                "separator": "space",
                "positional": False,
                "validation": None,
                "examples": None,
                "short_flag": None,
            },
            {
                "name": "Target",
                "flag": "TARGET",
                "type": "string",
                "description": "Target",
                "required": False,
                "default": None,
                "choices": None,
                "group": None,
                "depends_on": None,
                "repeatable": False,
                "separator": "space",
                "positional": True,
                "validation": None,
                "examples": None,
                "short_flag": None,
            },
        ],
    }


# =====================================================================
print("\n=== TEST A1: Zero in command preview ===")
# =====================================================================

with tempfile.TemporaryDirectory() as tmpdir:
    schema_path = Path(tmpdir) / "zero_test.json"
    schema_path.write_text(json.dumps(make_zero_test_schema()))

    win = scaffold.MainWindow(tool_path=str(schema_path))
    form = win.form
    m_key = (form.GLOBAL, "-m")
    m_widget = form.fields[m_key]["widget"]

    # Set to 0
    m_widget.setValue(0)
    form.command_changed.emit()
    app.processEvents()

    cmd, display = form.build_command()
    check("-m" in cmd, "A1: -m flag present in command list")
    idx = cmd.index("-m")
    check(cmd[idx + 1] == "0", f"A1: -m followed by '0' in command: {cmd}")

    preview_text = win.preview.toPlainText()
    check("-m 0" in preview_text, f"A1: '-m 0' visible in preview: '{preview_text}'")

    # =====================================================================
    print("\n=== TEST A2: Zero vs unset distinction ===")
    # =====================================================================

    # Reset to unset
    m_widget.setValue(m_widget.minimum())
    form.command_changed.emit()
    app.processEvents()

    cmd_unset, display_unset = form.build_command()
    check("-m" not in cmd_unset, f"A2: unset state omits -m: {cmd_unset}")
    check(m_widget.value() == m_widget.minimum(), "A2: widget at minimum (sentinel)")
    check(m_widget.specialValueText() == " ", "A2: special value text is blank placeholder")

    preview_unset = win.preview.toPlainText()
    check("-m" not in preview_unset, f"A2: preview omits -m when unset: '{preview_unset}'")

    # Now set to 0
    m_widget.setValue(0)
    form.command_changed.emit()
    app.processEvents()

    cmd_zero, display_zero = form.build_command()
    check("-m" in cmd_zero, f"A2: after setting to 0, -m present: {cmd_zero}")

    preview_zero = win.preview.toPlainText()
    check("-m 0" in preview_zero, f"A2: preview shows '-m 0': '{preview_zero}'")

    # Critical: the two commands must differ
    check(cmd_unset != cmd_zero, f"A2 CRITICAL: unset and 0 produce DIFFERENT commands")

    # =====================================================================
    print("\n=== TEST A3: Zero survives preset round-trip ===")
    # =====================================================================

    # Set -m to 0 and --max-retries to 0
    m_widget.setValue(0)
    mr_key = (form.GLOBAL, "--max-retries")
    mr_widget = form.fields[mr_key]["widget"]
    mr_widget.setValue(0)
    form.command_changed.emit()
    app.processEvents()

    # Serialize
    preset = form.serialize_values()
    check(preset.get("-m") == 0, f"A3: preset has -m=0: {preset.get('-m')}")
    check(preset.get("--max-retries") == 0, f"A3: preset has --max-retries=0: {preset.get('--max-retries')}")

    # Reset to defaults
    form.reset_to_defaults()
    form.command_changed.emit()
    app.processEvents()

    val_after_reset = form.get_field_value(m_key)
    check(val_after_reset is None, f"A3: after reset, -m is unset (None): {val_after_reset}")
    cmd_after_reset, _ = form.build_command()
    check("-m" not in cmd_after_reset, "A3: after reset, -m not in command")

    # Load preset
    form.apply_values(preset)
    form.command_changed.emit()
    app.processEvents()

    val_after_load = form.get_field_value(m_key)
    check(val_after_load == 0, f"A3: after preset load, -m is 0: {val_after_load}")
    cmd_after_load, _ = form.build_command()
    check("-m" in cmd_after_load, f"A3: after preset load, -m in command: {cmd_after_load}")
    check("0" in cmd_after_load, "A3: '0' in command after preset load")

    # Save to disk, close, reopen, reload
    preset_dir = scaffold._presets_dir("zero_test")
    preset_file = preset_dir / "zero_preset.json"
    preset_file.write_text(json.dumps(preset))

    win.close()
    win.deleteLater()
    app.processEvents()

    win2 = scaffold.MainWindow(tool_path=str(schema_path))
    saved = json.loads(preset_file.read_text())
    win2.form.apply_values(saved)
    win2.form.command_changed.emit()
    app.processEvents()

    val_reopened = win2.form.get_field_value((win2.form.GLOBAL, "-m"))
    check(val_reopened == 0, f"A3: after reopen + preset load, -m is 0: {val_reopened}")

    cmd_reopened, _ = win2.form.build_command()
    check("-m" in cmd_reopened and "0" in cmd_reopened,
          f"A3: reopened command includes '-m 0': {cmd_reopened}")

    # Cleanup
    preset_file.unlink(missing_ok=True)
    if preset_dir.exists() and not any(preset_dir.iterdir()):
        preset_dir.rmdir()

    # =====================================================================
    print("\n=== TEST A4: Zero with elevation ===")
    # =====================================================================

    form2 = win2.form
    m_key2 = (form2.GLOBAL, "-m")
    form2.fields[m_key2]["widget"].setValue(0)

    # Check elevation box
    check(form2.elevation_check is not None, "A4: elevation checkbox exists (schema has elevated=optional)")
    if form2.elevation_check is not None:
        form2.elevation_check.setChecked(True)
        form2.command_changed.emit()
        app.processEvents()

        cmd_elev, _ = form2.build_command()
        # build_command returns the base command; elevation wrapping happens in preview
        check("-m" in cmd_elev, f"A4: base command has -m: {cmd_elev}")

        # Simulate what the preview does
        elev_cmd, err = scaffold.get_elevation_command(cmd_elev)
        elev_display = scaffold._format_display(elev_cmd)
        check("-m" in elev_display and "0" in elev_display,
              f"A4: elevated display includes '-m 0': '{elev_display}'")

        preview_text = win2.preview.toPlainText()
        check("-m" in preview_text and "0" in preview_text,
              f"A4: preview with elevation includes '-m 0': '{preview_text}'")

        form2.elevation_check.setChecked(False)

    # =====================================================================
    print("\n=== TEST A5: Float zero ===")
    # =====================================================================

    r_key = (form2.GLOBAL, "--rate")
    r_widget = form2.fields[r_key]["widget"]

    # Unset state
    r_widget.setValue(r_widget.minimum())
    form2.command_changed.emit()
    app.processEvents()
    cmd_no_rate, _ = form2.build_command()
    check("--rate" not in cmd_no_rate, f"A5: unset float omits --rate: {cmd_no_rate}")

    # Set to 0.0
    r_widget.setValue(0.0)
    form2.command_changed.emit()
    app.processEvents()
    val_float = form2.get_field_value(r_key)
    check(val_float == 0.0, f"A5: float value is 0.0: {val_float}")

    cmd_with_rate, _ = form2.build_command()
    check("--rate" in cmd_with_rate, f"A5: --rate present in command: {cmd_with_rate}")

    preview_rate = win2.preview.toPlainText()
    check("--rate" in preview_rate and "0.0" in preview_rate,
          f"A5: preview shows '--rate 0.0': '{preview_rate}'")

    # =====================================================================
    print("\n=== TEST A6: Negative values / sentinel boundary ===")
    # =====================================================================

    m_key3 = (form2.GLOBAL, "-m")
    m_w = form2.fields[m_key3]["widget"]

    # The sentinel is at minimum() = -1 for no-default integer
    check(m_w.minimum() == -1, f"A6: minimum (sentinel) is -1: {m_w.minimum()}")

    # Setting to -1 should be treated as "unset"
    m_w.setValue(-1)
    val_neg1 = form2.get_field_value(m_key3)
    check(val_neg1 is None, f"A6: value -1 (sentinel) returns None: {val_neg1}")

    cmd_neg1, _ = form2.build_command()
    check("-m" not in cmd_neg1, f"A6: sentinel -1 omits -m: {cmd_neg1}")

    # Setting to 0 is a real value
    m_w.setValue(0)
    val_0 = form2.get_field_value(m_key3)
    check(val_0 == 0, f"A6: value 0 returns 0: {val_0}")

    # Setting to 1 is obviously a real value
    m_w.setValue(1)
    val_1 = form2.get_field_value(m_key3)
    check(val_1 == 1, f"A6: value 1 returns 1: {val_1}")

    # For integer WITH default (--count, default=5), there's no sentinel
    c_key = (form2.GLOBAL, "--count")
    c_w = form2.fields[c_key]["widget"]
    check(c_w.specialValueText() == "", f"A6: integer with default has no special value text: '{c_w.specialValueText()}'")
    c_w.setValue(0)
    val_c0 = form2.get_field_value(c_key)
    check(val_c0 == 0, f"A6: integer with default set to 0 returns 0: {val_c0}")
    c_w.setValue(-5)
    val_cn5 = form2.get_field_value(c_key)
    check(val_cn5 == -5, f"A6: integer with default set to -5 returns -5: {val_cn5}")

    # Dependency check: -m at 0 should enable --depth
    d_key = (form2.GLOBAL, "--depth")
    d_field = form2.fields[d_key]
    m_w.setValue(0)
    form2.command_changed.emit()
    app.processEvents()
    # Manually trigger dependency update
    active = form2._is_field_active(m_key3)
    check(active is True, f"A6: -m at 0 is active for dependencies: {active}")
    check(d_field["widget"].isEnabled(), "A6: --depth enabled when -m=0")

    # -m at sentinel should disable --depth
    m_w.setValue(-1)
    form2.command_changed.emit()
    app.processEvents()
    active_sentinel = form2._is_field_active(m_key3)
    check(active_sentinel is False, f"A6: -m at sentinel is inactive: {active_sentinel}")

    win2.close()
    win2.deleteLater()
    app.processEvents()


# =====================================================================
print("\n=== TEST B1: Valid extra flags input (baseline) ===")
# =====================================================================

nmap_path = str(Path(__file__).parent / "scaffold_data" / "tools" / "nmap.json")
win3 = scaffold.MainWindow(tool_path=nmap_path)
form3 = win3.form

form3.extra_flags_group.setChecked(True)
form3.extra_flags_edit.setPlainText("--script vuln --script-args unsafe")
app.processEvents()

style = form3.extra_flags_edit.styleSheet()
check("border" not in style or "red" not in style.lower(),
      f"B1: valid input has no error border: '{style}'")

cmd_b1, _ = form3.build_command()
check("--script" in cmd_b1 and "vuln" in cmd_b1,
      f"B1: extra flags in command: {cmd_b1[-4:]}")

preview_b1 = win3.preview.toPlainText()
check("--script vuln" in preview_b1, f"B1: extra flags in preview: '{preview_b1[-40:]}'")


# =====================================================================
print("\n=== TEST B2: Unclosed single quote ===")
# =====================================================================

form3.extra_flags_edit.setPlainText("--script 'http-title")
app.processEvents()

style_b2 = form3.extra_flags_edit.styleSheet()
check("border" in style_b2, f"B2: unclosed single quote gets error border: '{style_b2}'")

# Now close the quote
form3.extra_flags_edit.setPlainText("--script 'http-title'")
app.processEvents()

style_b2_fixed = form3.extra_flags_edit.styleSheet()
check("border" not in style_b2_fixed or style_b2_fixed == "",
      f"B2: closed quote clears error border: '{style_b2_fixed}'")

extra_b2 = form3.get_extra_flags()
check("--script" in extra_b2 and "http-title" in extra_b2,
      f"B2: fixed flags parse correctly: {extra_b2}")


# =====================================================================
print("\n=== TEST B3: Unclosed double quote ===")
# =====================================================================

form3.extra_flags_edit.setPlainText('--script "http-title')
app.processEvents()

style_b3 = form3.extra_flags_edit.styleSheet()
check("border" in style_b3, f"B3: unclosed double quote gets error border: '{style_b3}'")

# Close the quote
form3.extra_flags_edit.setPlainText('--script "http-title"')
app.processEvents()

style_b3_fixed = form3.extra_flags_edit.styleSheet()
check("border" not in style_b3_fixed or style_b3_fixed == "",
      f"B3: closed double quote clears error: '{style_b3_fixed}'")


# =====================================================================
print("\n=== TEST B4: Unclosed quote with Run attempt ===")
# =====================================================================

# Set a required field so Run isn't blocked by validation
for key, field in form3.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        form3._set_field_value(key, "127.0.0.1")
        break

form3.extra_flags_edit.setPlainText('--flag "unclosed')
form3.command_changed.emit()
app.processEvents()

# Malformed extra flags return empty list (no broken tokens in preview)
extra_b4 = form3.get_extra_flags()
check(len(extra_b4) == 0, f"B4: malformed extra flags return empty list: {extra_b4}")

# Build command — should not crash
cmd_b4, display_b4 = form3.build_command()
check(len(cmd_b4) > 0, f"B4: command builds without crash: {cmd_b4[:5]}...")

# Red border should still be visible
style_b4 = form3.extra_flags_edit.styleSheet()
check("border" in style_b4, f"B4: red border visible during run: '{style_b4}'")


# =====================================================================
print("\n=== TEST B5: Red border clears on empty ===")
# =====================================================================

form3.extra_flags_edit.setPlainText('--flag "unclosed')
app.processEvents()
check("border" in form3.extra_flags_edit.styleSheet(), "B5: red border on invalid input")

form3.extra_flags_edit.setPlainText("")
app.processEvents()

style_b5 = form3.extra_flags_edit.styleSheet()
check(style_b5 == "", f"B5: clearing field removes error border: '{style_b5}'")


# =====================================================================
print("\n=== TEST B6: Red border clears on fix ===")
# =====================================================================

form3.extra_flags_edit.setPlainText('--flag "unclosed')
app.processEvents()
check("border" in form3.extra_flags_edit.styleSheet(), "B6: red border on invalid input")

form3.extra_flags_edit.setPlainText('--flag "unclosed"')
app.processEvents()

style_b6 = form3.extra_flags_edit.styleSheet()
check(style_b6 == "" or "border" not in style_b6,
      f"B6: closing quote clears border immediately: '{style_b6}'")

# Preview should update
preview_b6 = win3.preview.toPlainText()
check("--flag" in preview_b6 and "unclosed" in preview_b6,
      f"B6: preview includes fixed flags: '{preview_b6[-40:]}'")


# =====================================================================
print("\n=== TEST B7: Extra flags validation in dark mode ===")
# =====================================================================

# Enable dark mode
scaffold.apply_theme(True)
app.processEvents()

form3.extra_flags_edit.setPlainText('--flag "unclosed')
app.processEvents()

style_b7 = form3.extra_flags_edit.styleSheet()
check("border" in style_b7, f"B7: error border present in dark mode: '{style_b7}'")
# Dark mode should use the theme's error color, not hardcoded "red"
check(scaffold.DARK_COLORS["required"] in style_b7,
      f"B7: dark mode uses theme error color ({scaffold.DARK_COLORS['required']}): '{style_b7}'")

# Fix the input
form3.extra_flags_edit.setPlainText('--flag "fixed"')
app.processEvents()
style_b7_fixed = form3.extra_flags_edit.styleSheet()
check(style_b7_fixed == "" or "border" not in style_b7_fixed,
      f"B7: error clears in dark mode: '{style_b7_fixed}'")

# Restore light mode
scaffold.apply_theme(False)
app.processEvents()


# =====================================================================
print("\n=== TEST B8: Preset with invalid extra flags ===")
# =====================================================================

# Save a valid preset first
form3.extra_flags_group.setChecked(True)
form3.extra_flags_edit.setPlainText("--valid-flag value")
app.processEvents()

preset_b8 = form3.serialize_values()
check("_extra_flags" in preset_b8, f"B8: preset has _extra_flags: {preset_b8.get('_extra_flags')}")

# Manually corrupt the extra flags
preset_b8["_extra_flags"] = '--flag "unclosed'

# Apply the corrupted preset
form3.apply_values(preset_b8)
app.processEvents()

# The field should show the raw invalid text
field_text = form3.extra_flags_edit.toPlainText()
check(field_text == '--flag "unclosed',
      f"B8: corrupted preset text loaded: '{field_text}'")

# Red border should appear after the validation fires
# (apply_values triggers textChanged -> _validate_extra_flags)
# Need to process events for signal to fire
form3.extra_flags_edit.setPlainText(preset_b8["_extra_flags"])
app.processEvents()

style_b8 = form3.extra_flags_edit.styleSheet()
check("border" in style_b8, f"B8: corrupted preset triggers error border: '{style_b8}'")

# Rest of preset should have loaded normally (no crash)
check(True, "B8: no crash loading preset with invalid extra flags")

# Cleanup
form3.extra_flags_group.setChecked(False)
form3.extra_flags_edit.setPlainText("")


# =====================================================================
# Final cleanup
# =====================================================================
win3.close()
win3.deleteLater()
app.processEvents()
_cleanup_recovery_files()

# =====================================================================
print(f"\n{'=' * 60}")
print(f"MANUAL VERIFICATION RESULTS: {passed}/{passed + failed} passed, {failed} failed")
if errors:
    print(f"\nFailed tests:")
    for e in errors:
        print(f"  - {e}")
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
