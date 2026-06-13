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

"""Quick Smoke Test — Pre-Deploy Sanity Check.

Confirms nothing is broken after recent changes. Does not refactor or optimize.
Covers: launch/load, form/preview, command execution, preset round-trip, window behavior.
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path

# Fix Unicode output on Windows (cp1252 can't encode ▼/▾ characters)
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
    QLineEdit, QPlainTextEdit, QListWidget, QLabel,
)
from PySide6.QtCore import Qt, QSettings, QProcess

app = QApplication.instance() or QApplication(sys.argv)

from PySide6.QtWidgets import QMessageBox
import scaffold

# Auto-decline recovery prompts so stale recovery files don't block tests
QMessageBox.question = lambda *a, **kw: QMessageBox.StandardButton.No

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
print("\n=== 1 — Launch and Load ===")
# =====================================================================

# 1a. Launch with no args — picker appears
win = scaffold.MainWindow()
win._show_picker()
app.processEvents()

check(win.stack.currentIndex() == 0, "tool picker visible on launch")
check(win.picker.table.rowCount() > 0, f"picker shows {win.picker.table.rowCount()} tools")

# Verify no empty/broken rows
for row in range(win.picker.table.rowCount()):
    item = win.picker.table.item(row, 1)
    check(item is not None and len(item.text()) > 0,
          f"  row {row} has tool name: {item.text() if item else 'NONE'}")

# 1b. Pick a tool — form loads
nmap_path = str(Path(__file__).parent / "scaffold_data" / "tools" / "nmap.json")
win._load_tool_path(nmap_path)
app.processEvents()

check(win.stack.currentIndex() == 1, "form view loaded")
check(win.form is not None, "form widget exists")
check(len(win.form.fields) > 0, f"form has {len(win.form.fields)} fields")

# Verify form layout — header, form frame, preview, output all exist
check(hasattr(win, 'form_frame'), "form frame exists")
check(hasattr(win, 'preview'), "preview bar exists")
check(hasattr(win, 'output'), "output panel exists")
check(hasattr(win, 'run_btn'), "run button exists")
check(hasattr(win, 'output_handle'), "drag handle exists")

# 1c. Light mode — check widgets render
scaffold.apply_theme(False)
app.processEvents()
check(scaffold._dark_mode is False, "light mode active")

# Verify key widgets have readable text (not empty labels)
header_found = False
for child in win.form.children():
    if isinstance(child, QLabel) and "nmap" in child.text().lower():
        header_found = True
        break
check(header_found, "form header label contains tool name")

# Check preview bar has placeholder or content
check(win.preview.toPlainText() != "" or win.preview.placeholderText() != "",
      "preview bar has content or placeholder")

# 1d. Dark mode — toggle and verify
scaffold.apply_theme(True)
win._apply_widget_theme()
app.processEvents()
check(scaffold._dark_mode is True, "dark mode active")

# Verify output panel has dark styling
output_style = win.output.styleSheet()
check(scaffold.DARK_COLORS['output_bg'] in output_style,
      f"output panel has dark background color")

# Verify preview has dark styling
preview_style = win.preview.styleSheet()
check(scaffold.DARK_COLORS['widget'] in preview_style,
      "preview bar has dark styling")

# Toggle back to light
scaffold.apply_theme(False)
win._apply_widget_theme()
app.processEvents()
check(scaffold._dark_mode is False, "back to light mode cleanly")

# Verify light styling restored
output_style_light = win.output.styleSheet()
check(scaffold.OUTPUT_BG in output_style_light,
      "output panel restored to light styling")


# =====================================================================
print("\n=== 2 — Form and Preview ===")
# =====================================================================

form = win.form

# 2a. Fill a checkbox
checkbox_key = None
for key, field in form.fields.items():
    if field["arg"]["type"] == "boolean" and not field["arg"]["group"]:
        checkbox_key = key
        field["widget"].setChecked(True)
        check(field["widget"].isChecked(), f"checkbox '{field['arg']['name']}' responds to input")
        break

# 2b. Fill a dropdown (enum)
enum_key = None
for key, field in form.fields.items():
    if field["arg"]["type"] == "enum":
        enum_key = key
        w = field["widget"]
        if w.count() > 1:
            w.setCurrentIndex(1)
            check(w.currentIndex() == 1, f"dropdown '{field['arg']['name']}' responds to selection")
        break

# 2c. Fill a text input (string)
string_key = None
for key, field in form.fields.items():
    if field["arg"]["type"] == "string" and not field["arg"].get("examples"):
        string_key = key
        w = field["widget"]
        if isinstance(w, QLineEdit):
            w.setText("test_value")
            check(w.text() == "test_value", f"text input '{field['arg']['name']}' responds to typing")
        break

# 2d. Fill the required positional (Target)
target_key = None
for key, field in form.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        target_key = key
        form._set_field_value(key, "192.168.1.1")
        break

form.command_changed.emit()
app.processEvents()

# 2e. Live preview updates
preview_text = win.preview.toPlainText()
check(len(preview_text) > 0, f"preview has content: '{preview_text[:60]}...'")
check("nmap" in preview_text, "preview starts with binary name")
if target_key:
    check("192.168.1.1" in preview_text, "preview contains target value")

# 2f. Filled fields appear in command
cmd, display = form.build_command()
check(cmd[0] == "nmap", "command starts with nmap")
if checkbox_key:
    flag = form.fields[checkbox_key]["arg"]["flag"]
    check(flag in cmd, f"checked flag '{flag}' in command")

# 2g. Copy command
win._copy_command()
clipboard = QApplication.clipboard().text()
check("nmap" in clipboard, f"clipboard has command after copy: '{clipboard[:50]}...'")


# =====================================================================
print("\n=== 3 — Run a Command ===")
# =====================================================================

# Load ping for a fast safe command
ping_path = str(Path(__file__).parent / "scaffold_data" / "tools" / "ping.json")
win._load_tool_path(ping_path)
form = win.form
app.processEvents()

# Set target
for key, field in form.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        form._set_field_value(key, "127.0.0.1")
        break

# Set count to 1
for key, field in form.fields.items():
    if field["arg"]["flag"] in ("-c", "-n"):
        w = field["widget"]
        if isinstance(w, QSpinBox):
            w.setValue(1)
        elif isinstance(w, QLineEdit):
            w.setText("1")
        break

form.command_changed.emit()
app.processEvents()

# Run
win._clear_output()
win._on_run_stop()
app.processEvents()

check(win.run_btn.text() == "Stop", "run button shows 'Stop' during execution")
check(win.process is not None, "QProcess created")

# Wait for completion
win.process.waitForFinished(10000)
app.processEvents()
win._flush_output()
app.processEvents()

output_text = win.output.toPlainText()
check(len(output_text) > 0, f"output panel has content ({len(output_text)} chars)")
check("$" in output_text, "output shows command echo ($ prefix)")

# Exit code color — check for exit status message
check("exited with code" in output_text.lower(), "exit code message present")

# Run button restored
check(win.run_btn.text() == "Run", "run button restored to 'Run'")

# Verify output colors are set (text cursor has formatting)
cursor = win.output.textCursor()
cursor.movePosition(cursor.MoveOperation.Start)
check(cursor.charFormat().foreground().color().isValid(),
      "output text has color formatting applied")


# =====================================================================
print("\n=== 4 — Preset Round-Trip ===")
# =====================================================================

# Load nmap for preset testing
win._load_tool_path(nmap_path)
form = win.form
app.processEvents()

# Fill several fields
for key, field in form.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        form._set_field_value(key, "10.0.0.0/8")
        break

sS_key = None
for key, field in form.fields.items():
    if field["arg"]["flag"] == "-sS":
        field["widget"].setChecked(True)
        sS_key = key
        break

form.command_changed.emit()
app.processEvents()

# 4a. Save preset
preset_data = form.serialize_values()
check(len(preset_data) > 0, f"preset serialized: {len(preset_data)} entries")

preset_dir = scaffold._presets_dir("nmap")
preset_file = preset_dir / "_smoke_test.json"
preset_file.write_text(json.dumps(preset_data, indent=2))
check(preset_file.exists(), "preset file written")

# 4b. Reset to defaults
form.reset_to_defaults()
form.command_changed.emit()
app.processEvents()

if sS_key:
    check(not form.fields[sS_key]["widget"].isChecked(), "-sS unchecked after reset")

for key, field in form.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        val = form._raw_field_value(key)
        check(val is None or val == "", f"target cleared after reset: {val}")
        break

# 4c. Load preset — fields restore
saved = json.loads(preset_file.read_text())
form.apply_values(saved)
form.command_changed.emit()
app.processEvents()

if sS_key:
    check(form.fields[sS_key]["widget"].isChecked(), "-sS restored from preset")

for key, field in form.fields.items():
    if field["arg"]["positional"] and field["arg"]["required"]:
        val = form._raw_field_value(key)
        check(val == "10.0.0.0/8", f"target restored from preset: {val}")
        break

# Verify command preview matches
cmd, _ = form.build_command()
check("-sS" in cmd, "-sS in command after preset load")
check("10.0.0.0/8" in cmd, "target in command after preset load")

# Cleanup
preset_file.unlink(missing_ok=True)
if preset_dir.exists() and not any(preset_dir.iterdir()):
    preset_dir.rmdir()


# =====================================================================
print("\n=== 5 — Window Behavior ===")
# =====================================================================

# 5a. Resize small (minimum is 530x400)
win.resize(530, 400)
app.processEvents()
check(win.width() <= 550, f"window accepts small resize: {win.width()}x{win.height()}")

# Scroll area should still work
check(hasattr(form, 'scroll_layout'), "scroll layout exists for small window")

# 5b. Resize large
win.resize(1200, 900)
app.processEvents()
check(win.width() >= 1100, f"window accepts large resize: {win.width()}x{win.height()}")

# 5c. Geometry persistence
win.resize(850, 650)
app.processEvents()
win.settings.setValue("window/geometry", win.saveGeometry())

win2 = scaffold.MainWindow()
app.processEvents()
check(abs(win2.width() - 850) < 60, f"width persisted: {win2.width()} (expected ~850)")
check(abs(win2.height() - 650) < 60, f"height persisted: {win2.height()} (expected ~650)")

# 5d. Theme persistence
win.settings.setValue("appearance/theme", "dark")
pref = win.settings.value("appearance/theme")
check(pref == "dark", "theme preference persisted")
win.settings.setValue("appearance/theme", "system")  # cleanup

win2.close()
win2.deleteLater()


# =====================================================================
# Final cleanup
# =====================================================================
win.close()
win.deleteLater()
app.processEvents()
_cleanup_recovery_files()

print(f"\n{'=' * 60}")
print(f"SMOKE TEST RESULTS: {passed}/{passed + failed} passed, {failed} failed")
if errors:
    print(f"\nFailed tests:")
    for e in errors:
        print(f"  - {e}")
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
