# Changelog

All notable changes to Scaffold are documented here.


## [v2.13.1] — 2026-04-26

Headless `--run-cascade` now honors the saved `loop_mode` flag from the cascade JSON, with a safety rail. v2.13.0 silently dropped the flag and always ran a single iteration unless `--loop N` was supplied — the reasoning was CI safety, since honoring `loop_mode=true` from a saved file would mean infinite loops in cron jobs. The silent drop surprised users who built a cascade in the GUI with loop on and expected the same intent to carry over headlessly. This release surfaces the saved intent without compromising automation safety: if the cascade has `loop_mode=true`, `--loop N` is now **required** rather than ignored. Absent or false `loop_mode` continues to default to a single iteration.

**Migration:** scripts that ran cascades with `loop_mode=true` and got a single iteration under v2.13.0 will now exit 1 with an explanatory message. Add `--loop N` to the invocation to set the iteration count explicitly.

### Changed

- **Headless `loop_mode` is now honored as a required `--loop N` count.** Behavior matrix:

  | Saved `loop_mode` | `--loop N` provided | Behavior |
  |---|---|---|
  | absent or `false` | no | Run once (unchanged) |
  | absent or `false` | yes | Run N times (unchanged) |
  | `true` | no | **Exit 1 with explanatory stderr** (new) |
  | `true` | yes | Run N times (unchanged) |

  The exit-1 message is two lines: an actionable summary first, then the why so users don't think Scaffold is being arbitrary. Exit code is 1 — consistent with other "missing required input" failures like missing variables.

- **`--help` text for `--loop N` updated** to reflect the new rule: required when the cascade's saved `loop_mode` is on, otherwise defaults to 1.

- **`_parse_run_cascade_args` default for `loop_count` changed from `1` to `None`.** Lets `main()` distinguish "user didn't pass `--loop`" from "user passed `--loop 1`". The runner is unchanged — it still receives a concrete integer. §209A.1's default-loop_count assertion updated in lockstep.

- **`main()` peeks at the cascade JSON before constructing the runner** to read `loop_mode`. File-read, JSON-parse, and wrong-format failures all defer to the runner's existing error handling so the new check doesn't change stderr for unrelated bad-file cases. A non-cascade JSON with a stray `loop_mode` key still surfaces the runner's "not a valid cascade file" message, not the `loop_mode` rule.

### Tests

New §210 in `test_functional.py` pins the parser change (`loop_count` defaults to `None` when `--loop` is absent; `--loop N` still produces `N`) and covers the full `loop_mode` × `--loop` matrix via in-process `main()` invocation: `loop_mode=true` with no `--loop` exits 1 with stderr naming the rule; `loop_mode=true` with `--loop 5` runs 5 iterations; `loop_mode=false` with and without `--loop` runs 1 / N respectively (regression guards); a legacy cascade missing the `loop_mode` key entirely runs once. Iteration counts are verified via the `--summary` JSON readback so the new tests don't reach into runner internals.

#### Full suite results

- **All 6 test suites pass: 3,798/3,798 assertions, 0 failures**
  - Functional: 3,299/3,299 (+17, includes new §210 `loop_mode` coverage)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.13.0] — 2026-04-26

Headless cascade execution. Saved cascades can now run from the command line via `python scaffold.py --run-cascade <cascade.json>` without launching the GUI — for cron jobs, CI pipelines, batch processing, and other automation use cases. The headless runner shares the exact chain code path used by GUI cascade execution: the GUI is constructed in memory but never shown, so variable injection, capture extraction, stop-on-error, and preset hash gating all behave identically. Per-step `QProcess` output streams live to the parent's stdout/stderr with a `[step N: tool]` prefix, an optional JSON summary records per-step exit codes and timings, and a documented exit-code map (0 success, 2 stop-on-error halt, 3 non-zero step exit, 130 SIGINT) lets shell scripts branch on outcome without parsing logs.

### Added

- **`--run-cascade <cascade.json>` CLI flag.** Loads the cascade, validates dependencies, runs every step using the existing chain controller, and exits with a status code. Skips the GUI entirely; modal dialogs in the run path are intercepted and translated to fail-fast errors with stderr context. No silent "yes" responses to unexpected prompts.

- **Variable supply via `--var name=value` (repeatable) and `--vars file.json`.** `--var` accepts `=`-separated `name=value` pairs; `file.json` is a JSON object of `{name: value}`. Both can combine; CLI `--var` overrides `--vars` on conflict. Variable names must match the same `[A-Za-z_][A-Za-z0-9_]*` regex used for capture names. Cascades that define a variable but receive no value exit 1 with the missing names listed to stderr; extra (unreferenced) variables are silently ignored.

- **Loop control via `--loop N`.** Runs the cascade `N` times sequentially (range 1–1000; the cap prevents typos from turning into accidental infinite CI burns). The cascade JSON's own `loop_mode` flag is intentionally ignored in headless mode — outer iteration is controlled exclusively by `--loop`. With `stop_on_error=True`, a failing iteration halts the loop instead of continuing.

- **JSON summary via `--summary <path>`.** Writes a structured per-run record at exit: cascade name, terminal status (`completed` / `stopped` / `failed` / `interrupted`), ISO-8601 start/finish timestamps, total duration, loop count, and an array of per-step records (loop_index, step_index, tool, command, exit_code, duration, captures, error). Per-step stdout/stderr is intentionally **not** embedded — it's already streamed live and embedding multi-MB process output in a JSON file would defeat the streaming model. Summary write failure (e.g. unwritable target directory) is logged to stderr but does not change the run's exit code.

- **`CascadeHeadlessRunner` and `CascadeHeadlessAbort` classes.** The runner is a self-contained orchestrator: snapshots the QSettings keys it would otherwise pollute (`session/last_tool`, `picker/recent_tools`, `cascade/*`, `cascade_history/*`) before constructing `MainWindow`, restores them at exit. Patches `QMessageBox.{warning,critical,information,question}` to fail-fast (capture title/text to stderr, set abort flag, return a deterministic value to unblock callers). Patches `CascadeVariableDialog.exec` / `get_values` to bypass the GUI prompt and supply CLI-provided values directly. Replaces `_on_stdout_ready` / `_on_stderr_ready` with versions that stream prefixed lines instead of buffering for the (non-existent) GUI panel.

- **`_parse_run_cascade_args(argv)` helper.** Hand-rolled argv parsing matching the existing `--validate` / `--prompt` style — no `argparse` dependency. Returns `(parsed, error)` where `parsed` is a dict on success and `error` is a usage message on failure. Rejects unknown flags, missing required values, malformed `--var` specs, out-of-range `--loop` counts, and JSON parse failures from `--vars`.

- **SIGINT handling.** Ctrl+C during a headless run calls the cascade dock's `_on_stop_chain()` to terminate any active QProcess and exits with conventional status code 130. On Windows the SIGINT handler is best-effort (Python's signal model on Windows is more limited); on POSIX it's reliable.

### Changed

- **Exit code map for `--run-cascade`.** `0` = cascade completed, all steps exit 0, no capture failures. `1` = failed to start (parse error, missing dependency, missing variable, file-not-found, unexpected modal dialog). `2` = a step failed and `stop_on_error` was true → halted. `3` = cascade ran to completion but at least one step had non-zero exit (`stop_on_error` false). `130` = interrupted via SIGINT.

- **`--help` text now lists the headless flags.** `--run-cascade`, `--var`, `--vars`, `--loop`, and `--summary` appear in the usage block alongside the existing flags.

- **Broad-except budget raised from 11 to 15.** §189c's regression guard previously capped `scaffold.py` at 11 broad `except Exception:` catches. The headless runner adds 4 new sites that legitimately need to stay broad: MainWindow construction (touches QSettings, QTimer, and file I/O across many subsystems), `_teardown_process` cleanup (Qt-side errors during destruction), the SIGINT signal handler (must never raise back into the runtime), and the `_on_run_chain` wrapper (the cascade dock's chain controller calls into QProcess, file I/O, validation, and form rendering across many helper paths). Three other added catches were narrowed to specific types: `AttributeError` / `RuntimeError` on close + `deleteLater`, `OSError` / `TypeError` / `ValueError` on summary write, and `AttributeError` / `TypeError` on patch restoration.

### Tests

New §209 in `test_functional.py` covers argv parsing (positive and negative cases for every flag, including `--var` with `=` in the value, `--vars` JSON load, `--var` overriding `--vars`, `--loop` range bounds, unknown flags, missing required values), runner construction (loop_count clamping, missing cascade file, wrong or missing `_format`), variable handling (all-supplied, missing-named-in-stderr, extra-ignored), output streaming (stdout prefix, stderr `(err)` tag, multi-step prefix advancement), summary writing (file presence, schema fields, no-summary case, disk-write-failure code-unchanged), exit codes (all-zero → 0, stop_on_error halt → 2, no-stop with failure → 3, loop count multiplies records, loop + stop_on_error halts at first failing iteration), modal dialog suppression (missing tool dependency exits 1 without dialog, dialog patches restored after run), and state isolation (every tracked QSettings key restored to baseline after the run, including `cascade_history/entries`).

§189c's broad-except ceiling raised from 11 to 15 to accommodate the 4 new legitimate broad catches; floor stays at 9.

#### Full suite results

- **All 6 test suites pass: 3,784/3,784 assertions, 0 failures**
  - Functional: 3,283/3,283 (+94, includes new §209 headless coverage)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 80/80
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.12.3] — 2026-04-26

Per-history-entry notes annotation, plus two diagnostic dead-ends surfaced while landing the feature that are now hardened against. The orphaned-QSettings hang in particular had been silently waiting to bite anyone who killed a test mid-run; the PySide6 method-binding gotcha had been masking real assertion failures behind hangs in two different Section 208 attempts before being identified. Both are documented below so future-you can find the why.

### Fixed

- **Orphaned `session/last_tool` could hang the next process startup.** Sections 207 and 208 now clear `session/last_tool` on teardown. Both construct fresh `MainWindow` instances whose `__init__` implicitly reopens whatever path is stored under that key. If a prior section pointed it at a fixture without a `_format` marker (e.g. `tests/test_minimal.json`) and the process was killed before reaching final cleanup, the next launch — `test_smoke.py`, an ad-hoc run, or a re-invocation of this suite — would block at startup on the modal "Missing Format Marker" warning dialog. test_functional installs a `QMessageBox.warning` auto-accept patch, but no other entry point does, so the leaked key bricked everything else. Roughly 50 other sections leak the same key in normal flow without consequence (the next section overrides), but if any one of those is the last to fire before a kill, the same hang reappears. Closing those gaps systematically is parked for a follow-up cleanup pass.

- **`Klass.exec = func` does not intercept PySide6 instance calls.** Section 208's first context-menu and edit-dialog tests assigned to `QMenu.exec` and `QDialog.exec` at the class level expecting to capture instances; the assignment was a no-op for the blocking call path because Qt C++ methods are bound at instance construction, not resolved through Python attribute lookup at call time. Compounding the trap, `menu.exec()` blocks even on the offscreen Qt platform unless something closes the popup — so the silent no-op masked itself behind a hang. Section 208 now patches `QMenu.__init__` and `QDialog.__init__` to track instances, then drives popups via `QTimer.singleShot` (with an `aboutToShow`-driven close for menus). Worth pinning so future dialog/popup tests don't repeat the dead-end.

### Added

- **Per-history-entry user-editable notes.** The History dialog now shows a "Notes" column between Command and Time. Right-click any row and pick "Edit notes…" to attach context to a run ("ran during maintenance window," "this is the prod box," "preset variant for client X"). Notes participate in the dialog's filter bar so you can locate runs by their annotation, not just by command line. Double-click remains wired to Restore — intentionally not the edit affordance.

- **`MainWindow._update_history_notes(timestamp, notes)`.** Locates the history entry for the active tool whose `timestamp` matches and persists the new notes through the same `history/<tool>` QSettings key used by `_record_history_entry`. Returns `True` on a match, `False` otherwise; the no-op branch leaves storage untouched.

### Changed

- **History entries now carry a `notes` field.** New entries default to `""`. Legacy entries without the key render and filter as if `notes == ""` — the change is forward-compatible without bumping any history version key.

- **HistoryDialog table layout: 4 columns → 5.** Headers are now `["Exit", "Command", "Notes", "Time", "Age"]`. The Notes column at index 2 is interactive with a default width of 150px; Time and Age shift to indices 3 and 4. `_wire_last_column` and `_fit_last_column` derive the last column dynamically, so the shift needs no helper changes.

- **History filter now matches against Notes.** The case-insensitive substring search bar considers both the Command (col 1) and Notes (col 2) columns, so a filter needle hits if it appears in either.

### Tests

New §208 in `test_functional.py` covers: `notes` default on new entries, round-trip via QSettings, `_update_history_notes` match/no-match semantics, HistoryDialog 5-column header layout, legacy-entry rendering (missing `notes` key), Notes cell text and tooltip, Time/Age column shift to indices 3/4, context-menu invocation via programmatic `QPoint`, edit-dialog accept/cancel flow with cell + QSettings assertions, and filter behavior against the Notes column (case-insensitive, Notes-only positive, negative, and command-column regression). Pre-existing §49f / §49q assertions updated to reflect the new key set and the 5-column layout.

#### Full suite results

- **All 6 test suites pass: 3,690/3,690 assertions, 0 failures**
  - Functional: 3,189/3,189
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 80/80
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.12.2] — 2026-04-26

Picker quality-of-life: a "Recently used" section at the top of the
tool picker, and a first-run welcome modal that orients new users.
Both share the new `picker/*` QSettings namespace.

### Added
- "Recently used" virtual folder group at the top of the tool picker.
  Shows up to the 5 most-recently-loaded tools in MRU order, populated
  from the new `picker/recent_tools` QSettings key (JSON list, capped
  at 5). Each successful tool load via `_load_tool_path` records the
  path; failed loads, validation errors, and cancellations do not
  pollute the list. The same tool may appear twice — once under
  "Recently used", once in its real folder — by design. Stale paths
  (entries that no longer match any scanned tool) are filtered out
  silently. The section is omitted entirely when the recent list is
  empty.
- `WelcomeDialog`: a modal first-run dialog (480px fixed width)
  introducing what Scaffold does and pointing users at their starting
  options (pick a bundled tool, save a preset, toggle the Cascade dock
  with `Ctrl+G`). Surfaces the resolved `tools/` path so users know
  where to drop their own schemas. Includes a "Don't show again"
  checkbox (default checked) wired to the new
  `picker/welcome_dismissed` QSettings flag (`0` / `1`).

### Changed
- `ToolPicker._populate_table` now tracks a parallel `_row_folder`
  list alongside `_row_map` so each row knows its visual section, not
  just its underlying entry. This keeps per-section collapse and
  search filtering correct when the same tool is rendered in both
  "Recently used" and its real folder group.

### Tests
- Section 207 covers `_record_recent_tool` (persistence, dedupe, MRU
  ordering, 5-entry cap, corrupt-JSON recovery), the
  `_populate_table` rendering pass (header presence, empty-list
  omission, stale-path filtering, duplicate display, visual-folder
  tracking), and the `WelcomeDialog` lifecycle (instantiation,
  checked-vs-unchecked dismissal, second-launch suppression vs.
  first-launch trigger).
- `MainWindow._suppress_welcome_dialog`: class-level test escape
  hatch that bypasses the welcome-dialog timer entirely. Tests set
  it to `True` at startup so MainWindow construction never blocks on
  the modal. Section 207's 207C.6 / 207C.7 cases flip it off
  locally to exercise the real `__init__` branch (and restore it on
  exit). This avoids depending on QSettings persistence, which can
  flip between registry and INI backends mid-suite when section 205
  toggles portable mode.


## [v2.12.1] — 2026-04-25

File-format dispatch hardening: cascade-load and drag-and-drop now
route `.json` files by their `_format` marker instead of mis-handling
them.

### Fixed
- `_on_load_cascade_list` now rejects files whose `_format` is not
  `scaffold_cascade` with a critical dialog (matching the behavior of
  `_on_import_cascade`). Previously, only the dependency-check pass
  was gated on the format marker — non-cascade files slipped through
  to `_import_cascade_data`, which raised a generic ValueError.
- `dropEvent` now routes dropped `.json` files by their `_format`
  marker: presets apply to the open tool form (or show an
  informational dialog if no tool is open), cascades load through
  the cascade-import pipeline, and schemas / unknown formats fall
  through to `_load_tool_path`. Previously, every `.json` drop went
  to the schema loader, which surfaced the "wrong format" rejection
  for what should have been routine routing.

### Changed
- Extracted `_apply_preset_from_path` helper from `_on_load_preset`
  to support routing dropped preset files through the same
  validation pipeline (size guard, three-tier `_format` check,
  `validate_preset`, tool-identity confirmation, `apply_values`,
  schema-hash mismatch warning). `_on_load_preset` now opens the
  picker and delegates to the helper. No user-visible behavior
  change at the existing call site.

### Tests
- Section 206 covers cascade-load format rejection (schema/preset/
  missing-marker), drag-and-drop dispatch by `_format` (schema,
  preset with/without open tool, cascade, missing-marker fall-
  through), and the helper-extraction regression guard.


## [v2.12.0] — 2026-04-25

Pip-installable packaging. Scaffold can now be installed via
`pip install git+https://github.com/kevwillow/scaffold.git@v2.12.0`,
which provides a `scaffold` shell command. After install, user data
(presets, cascades, custom tool schemas) is stored in the platform's
standard application data directory; bundled files ship as a read-only
package within the wheel.

### Added
- `pyproject.toml` and console script entry: after install, `scaffold`
  is available as a shell command on the user's PATH.
- `scaffold_data/` directory holding bundled tool schemas, default
  presets, and LLM prompts. Ships as a Python package alongside
  `scaffold.py`.
- User-added tool schemas: drop `*.json` files into the platform's
  Scaffold app-data directory (Linux: `~/.local/share/Scaffold/tools/`,
  macOS: `~/Library/Application Support/Scaffold/tools/`, Windows:
  `%APPDATA%/Scaffold/tools/`) to make them appear in the picker
  alongside bundled tools. The picker shows whether each tool is
  bundled (read-only) or user-added (deletable).
- Mode-detection helpers (`_is_portable_mode`, `_is_installed_mode`)
  and a unified path-resolution layer (`_bundled_root`,
  `_user_data_root`, `_bundled_tools_dir`, `_resolve_app_relative`)
  that route to the correct filesystem location based on whether
  Scaffold is running from source, pip-installed, or in portable
  mode.

### Changed
- Bundled files (`tools/`, `default_presets/`, `SCHEMA_PROMPT.txt`,
  `PRESET_PROMPT.txt`, `CASCADE_LLM_GENERATION_GUIDE.md`) moved into
  `scaffold_data/`. The repo layout now separates code from bundled
  data, matching standard Python packaging.
- Tool picker merges user-added and bundled tools in one list, with
  user-added tools taking name precedence (so a user-supplied
  `nmap.json` overrides the bundled one).
- Tool delete action only operates on user-added schemas. Attempting
  to delete a bundled schema shows a status message indicating where
  to drop a custom override instead.
- Cascade-relative path resolution now tries the user data root first,
  then the bundled root, so cascades that reference `tools/foo.json`
  continue to work regardless of where the actual file lives.

### Fixed
- `print_prompt` was looking for `PROMPT.txt`, but the file is named
  `SCHEMA_PROMPT.txt`. Latent since an earlier rename. Now reads from
  the correct filename.
- `_user_data_root()` in installed mode now reliably lands on
  `<AppData>/Scaffold/`. `QStandardPaths.AppDataLocation` only auto-
  appends `QCoreApplication.applicationName()` when it has been set,
  but scaffold doesn't set the app name at startup, so the path was
  collapsing to bare `%APPDATA%` (or platform equivalent). The
  resolver now appends `Scaffold` when missing — idempotent if the
  app name is set later. Caught during Phase 5 install verification.

### Notes for users
- Source-clone behavior is unchanged. If you've been running
  `python scaffold.py` from a cloned repo, you don't need to do
  anything differently.
- Existing portable mode (placing `portable.txt` next to `scaffold.py`)
  still works unchanged.
- For pip-install users: your presets and cascades live in
  `<AppData>/Scaffold/`, not in site-packages. They survive
  uninstall/reinstall.

#### Full suite results
- **All 6 test suites pass: 3,617/3,617 assertions, 0 failures**
  - Functional: 3,118/3,118 (+36, includes new §205 path resolution contract)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.11.4] — 2026-04-24

Patch release: activate the v2.11.3 preset type rules at load callers,
audit shipped presets for silent-bug patterns, and consolidate version
test assertions.

### Added
- v2.11.3's per-key preset type validation now runs on production
  preset loads (was previously gated to test-only). Presets with type
  mismatches (e.g. `"false"` for a boolean field, list value for a
  string field) are now rejected at load with a user-visible error
  rather than producing silently-wrong form state. Wired at three
  callers: `_on_load_preset`, `_on_slot_clicked`, `_chain_advance`.
- `validate_preset` gains a `report_unknown_keys` parameter (default
  `True` for backward compat). Production load callers pass `False` so
  the schema_hash mismatch path remains the canonical stale-preset
  gate; legacy presets with renamed flags still load with the existing
  hash-mismatch warning rather than being hard-rejected.

### Changed
- Test version assertions consolidated to a single canonical
  `EXPECTED_VERSION` check at the top of `test_functional.py`.
  Previously each release added a new per-section assertion (§199G.1,
  §200H.1, §201J.1), requiring N-1 stale-string bumps per release.

### Fixed
- Audit of shipped default presets surfaced no further silent-bug
  patterns beyond the v2.11.3 rsync finding. All 56 bundled preset
  files (53 default_presets + 3 dev fixtures) pass both the activated-
  load-path validation and a four-pattern silent-bug audit (string
  field with list-repr value, boolean repeatable with int 0, empty
  multi_enum, surrounding whitespace). §204 encodes this as a
  permanent regression guard.

#### Full suite results
- **All 6 test suites pass: 3,581/3,581 assertions, 0 failures**
  - Functional: 3,082/3,082 (+15)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.11.3] — 2026-04-24

Patch release: tighten preset validation to reject hand-crafted or
LLM-generated presets that would otherwise produce silently-wrong form
state via Python's type coercion quirks.

### Fixed
- **D2b: Preset validation now rejects boolean fields with non-bool
  values** (e.g. `"--verbose": "false"` as a string). Previously,
  `bool("false") == True` meant such presets silently checked the box
  on load. Repeatable booleans (e.g. rsync `--verbose`, ssh `-v`) still
  accept `int` because the QSpinBox repeat count is the legitimate
  storage format for them.
- **D2c: Preset validation now rejects multi_enum fields with non-list
  values** (e.g. `"--ports": "80,443"` as a comma-separated string).
  Previously, such values were silently treated as empty selection.
- Per-key type enforcement now applies to all field types when a tool
  schema is available at validation time. Without a schema, structural
  validation is unchanged (backward compatible).
- `default_presets/rsync/rsync_selective_copy_excludes.json` was
  storing a list value for the `--exclude` string field, which the GUI
  could never apply cleanly (`str(value)` on a list produced garbage).
  Extra patterns now live in `_extra_flags`; the preset round-trips
  correctly under the new rule.

#### Full suite results
- **All 6 test suites pass: 3,566/3,566 assertions, 0 failures**
  - Functional: 3,067/3,067 (+73)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.11.2] — 2026-04-24

Patch release: size-cap history entries to prevent Windows registry
key-size limits from silently discarding history writes.

### Fixed
- R2-E4: Command history and cascade history entries are now size-capped
  at 64 KB per entry and 1 MB total. Oversized entries are preserved in
  the list with the command line and timestamp visible, but the stored
  preset_data is dropped and the entry cannot be restored to the form.
  This prevents the Windows registry backend from silently truncating
  history writes when a user runs commands with multi-MB extra_flags or
  large text-field values.

#### Full suite results
- **All 6 test suites pass: 3,493/3,493 assertions, 0 failures**
  - Functional: 2,994/2,994 (+42)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.11.1] — 2026-04-24

Interactive affordances and a timer-leak fix. Six small items that each sand off a rough edge in day-to-day use, plus one quiet bug (QTimer objects accumulating across tool reloads) that had been silently growing the window's child count for every schema switch.

### Added
- **Keyboard shortcuts for cascade controls.** Five window-global QShortcuts now operate on cascade state regardless of sidebar visibility: Add slot (Ctrl+Shift+A), Pause/Resume (Ctrl+P), Stop (Ctrl+.), Loop toggle (Ctrl+Shift+L), and Err toggle (Ctrl+Shift+E). The shortcuts route directly to `CascadeSidebar._on_add_slot` / `_on_pause_chain` / `_on_stop_chain` / `_toggle_loop` / `_toggle_stop_on_error`, so using the keyboard matches clicking the button exactly.
- **"Reset password copy prompt" menu entry.** Live under the File menu (after "Custom PATH Directories..."). Clicking it clears the remembered session-wide mask choice, so the next Copy triggers the Yes/No prompt again. Useful when you answered once and want a different answer now without restarting the app.
- **Copy status now shows whether passwords were masked or real.** The status-bar feedback that appears after a Copy ("Copied to clipboard." / "Copied as Bash.") now appends " — passwords included" or " — passwords masked" when the command contained password fields with values. No change in behavior when no passwords are involved — the suffix is suppressed entirely. This closes the visibility gap where the session-wide mask choice, once made, produced silent copies with no indication of whether the clipboard contained real secrets or `********`.

### Changed
- **File pickers now open in the directory of the current field value** instead of whichever directory Qt happened to pick. A new `_picker_start_dir()` helper resolves the line-edit's current value: if it's an existing file, the dialog opens in that file's parent; if it's an existing directory, it opens there; if the value is a plausible-looking path whose parent exists, it opens in the parent. Anything that doesn't resolve falls back to Qt's default (empty string). Both `_browse_file` and `_browse_directory` use the helper, so file and directory pickers behave consistently.
- **Subcommand combobox shows its description as a tooltip on the collapsed widget**, not just on the dropdown items. Previously, the per-item tooltip set via `setItemData(..., ToolTipRole)` only fired on items the user had opened the dropdown to see — the collapsed combobox had no tooltip at all. A second `currentIndexChanged` handler mirrors the current item's ToolTipRole onto the widget itself and primes the tooltip at form-build time, so hovering the closed combobox now reveals the same description you'd get by opening the dropdown and hovering the selected item. Subcommands with no description get an empty tooltip (not stale data from the previous selection).
- **Cascade Loop button now shows an explicit "Loop ✗" off-state.** The Err button has always shown "Err✓" when on and "Err✗" when off. The Loop button used "Loop ✓" when on but bare "Loop" when off — ambiguous about state. Both buttons now carry a glyph in both states, so the cascade control row reads symmetrically at a glance.

### Fixed
- **QTimer leak on form reload (R2-A2).** Three timers (`_status_timer`, `_timeout_timer`, `_flush_timer`) were created inside `_build_form_view` and parented to the MainWindow. Every tool reload created new timers and dereferenced the old ones, but Qt's parent link kept them alive — they accumulated on the window as the user switched between tools. The recurring `_flush_timer` was the worst offender because every stale instance continued firing. All three are now created once in `__init__` alongside the existing force-kill / elapsed / autosave cluster; `_build_form_view` just uses the already-existing attributes. The `_status_timer` lambda resolves `self.status` at fire time, so the safety invariant (the form view rebuilds the status label on every load) is preserved. After N reloads, exactly one of each timer lives on the window.

#### Full suite results
- **All 6 test suites pass: 3,451/3,451 assertions, 0 failures**
  - Functional: 2,952/2,952 (+91)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.11.0] — 2026-04-24

Messaging and status-bar release. Phase 1 split the status bar into two regions so a live progress counter no longer stomps sticky context messages. Phase 2 is a sweep of small wording and routing fixes across the messaging layer — each one user-visible, each one the result of a specific inaccuracy surfaced by the code audit.

### Changed
- **Status bar is now split into two regions.** The main (transient) region holds sticky context like "Loaded nmap — 42 fields", "Preset saved: default", and exit outcomes. A new right-aligned permanent label holds the live "Running... (Ns)" progress counter while a process runs. Before this change, the elapsed-seconds ticker called `statusBar().showMessage()` every second, which clobbered the load message within a second of clicking Run and left users with no visible context for the command they were actually watching. The two messages now live in different widgets and both survive for as long as they're relevant.

### Fixed
- **Elevation exit codes 126 and 127 now produce distinct, accurate messages.** The previous branch claimed "Elevation was cancelled by the user" for both codes, but 126 means "helper could not execute" (permission denied or binary-not-executable), and only 127 could plausibly be user cancellation. Exit 126 now reports "Elevation failed: helper could not execute." to the output panel in error color and "Elevation failed" to the status bar. Exit 127 reports "Elevation cancelled or unavailable" in warning color with the matching status message. Non-elevated runs continue to fall through to the generic non-zero exit branch unchanged.
- **Password-restore messaging now reflects reality.** Four call sites previously said "password fields were not restored", wording that implied passwords had been present in the preset and were dropped during load. Passwords are never saved in the first place — `serialize_values` skips them unconditionally at write time — so the old wording was misleading on both ends. All four sites now say "password fields must be re-entered", which is accurate regardless of what the user had typed before. `apply_values`'s docstring is updated to match: the return value indicates the schema *contains* password fields, not that any value was skipped.
- **"No presets" empty-state now uses the status bar instead of a modal.** Three other empty-state paths in the same class already routed through `statusBar().showMessage(...)`; this one was the outlier using `QMessageBox.information`. The modal required an extra click to dismiss for no information gain. Consistency with the other empty-state handlers restored. Modals for errors, confirmations, and destructive-action warnings are unchanged — they're load-bearing.
- **Tool-picker empty-state now shows the absolute tools directory path.** The previous "Place JSON schema files in the tools/ directory" text gave users no way to find where "tools/" actually is. The label now resolves the absolute path at construction time and displays it explicitly, so a user facing an empty picker can navigate straight to the right folder without digging through the filesystem layout.
- **`closeEvent` no longer emits a status message the user never sees.** When the main window closes mid-cascade, `_chain_cleanup` was being called with a "Closing" message that painted to the status bar for a few hundred milliseconds before the window disappeared. `_chain_cleanup` now accepts a `silent=False` keyword argument; `closeEvent` passes `silent=True` to skip the status write while still performing all of the state-reset work. All other callers are unchanged.
- **`_save_output` no longer uses inconsistent per-call message timeouts.** Three status messages in this method carried explicit 3000/3000/5000ms timeouts, an outlier pattern now that Phase 1's split-bar architecture means sticky messages no longer get stomped by the elapsed counter. Messages now persist until the next status update, matching every other sticky message in the file.

#### Full suite results
- **All 6 test suites pass: 3,360/3,360 assertions, 0 failures**
  - Functional: 2,861/2,861 (+56)
  - Security: 243/243
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.10] — 2026-04-22

Closes the v2.10.x cleanup arc. Eight coverage gaps surfaced by the 2026-04-20 deep code audit are now guarded against regression, and one validation helper's docstring is updated to document a subtle test-only contract.

### Fixed
- **Force-kill and timeout paths now have regression coverage.** The process-termination logic on the main window — SIGKILL escalation when a graceful stop is ignored, and the user-set timeout kill — was functionally working but completely untested. A silent regression in either path would have let runaway processes survive stop requests with no suite-level signal.
- **Oversized recovery file handling is now verified.** The bounded-size guard on startup recovery existed in code but had no regression test, meaning a future refactor could have removed the size cap and let a corrupted or malicious recovery file crash the app at launch.
- **Shipped-preset schema drift is now detected automatically.** Every default preset that carries a real schema hash is now cross-checked against the current hash of its tool schema on every test run. If a tool schema changes and a preset shipped against the old version isn't updated, the suite fails loudly instead of users hitting mysterious "flag not recognized" errors at load time. Presets using the `"00000000"` opt-out sentinel are respected as intentional skips.
- **Meta-key and meta-value length rejection is now guarded.** `validate_preset`'s length caps existed but nothing in the suite exercised them; a future change that accidentally raised or removed those caps would have shipped silently.
- **`display_group` non-string values are now rejected with a regression test.** The validation branch existed but was unreachable from any test, meaning a future simplification could have deleted it without the suite noticing.
- **Metachar passthrough is now verified for `separator="equals"` and `separator="none"`.** Prior security coverage tested only the default `"space"` separator's handling of shell metacharacters; the two alternate join paths reach `build_command` through different logic and had no regression guard.
- **Subcommand preset round-trips are now verified against real multi-subcommand schemas.** The subcommand-scoped key format (`sub_name:flag`) was only exercised by synthetic fixtures; the suite now loads docker_test schemas and openclaw, switches subcommands, sets values, serializes, re-applies, and verifies round-trip fidelity on real shipped schemas.

### Changed
- **`validate_preset`'s optional `tool_data=` argument is now documented as a test-only entrypoint.** Production callers omit this argument; its actual use is the shipped-presets cohort guard in `test_preset_validation.py §30`, which catches drift between newly-shipped default presets and their corresponding tool schemas. The docstring now says so plainly, so future cleanup work doesn't silently remove load-bearing validation infrastructure.

#### Full suite results
- **All 6 test suites pass: 3,304/3,304 assertions, 0 failures**
  - Functional: 2,805/2,805 (+36)
  - Security: 243/243 (+12)
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.9] — 2026-04-22

Refactor release. Six different dialogs each had their own copy of the method that keeps a table's last column filling the available width when a user resizes other columns. All six now share a single helper, with an optional extension hook for the one dialog that needs additional column-width enforcement.

### Changed
- **Last-column width management is now handled in one place.** Six dialogs (tool picker, preset picker, history, cascade history, cascade list, cascade variable definitions) each defined a small wrapper method that redistributed table space when a column was resized. The wrapper logic is now a single module-level helper; the one dialog that needs additional behavior (enforcing bounds on the Flag column) passes that behavior as a callback.

### Tests
New §194 in `test_functional.py` guards against regression: the helper is verified to exist with the expected signature, the six classes no longer define the old wrapper method, the extension hook is exercised with a real table widget, and an end-to-end nmap load confirms no regression in real tool rendering.

#### Full suite results
- **All 6 test suites pass: 3,256/3,256 assertions, 0 failures**
  - Functional: 2,769/2,769 (+29)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.8] — 2026-04-22

Refactor release. Four widget-construction code paths inside the tool-form renderer shared most of their logic but differed in one small thing each: the spinbox used for integer vs float fields, and the picker button wired up for file vs directory fields. Those four paths are now two helpers, one for each pair.

### Changed
- **Integer and float spinbox widgets are now built by a single helper.** The two types shared every structural detail — range setup, sentinel handling for fields with no default, min/max clamping, signal wiring — and differed only in widget class, numeric cast, and whether to show two decimal places. A single helper method handles both; the call sites are now one line each.
- **File and directory picker widgets are now built by a single helper.** Same story: both built an identical QLineEdit + "Browse..." button combination and only differed in which module-scope browse function the button invoked. One helper handles both cases via a keyword flag.

### Tests
New §193 in `test_functional.py` guards against regression: both helpers are verified to exist on ToolForm with the expected signatures, each is exercised with a range of minimal schemas to confirm the returned widget has the correct shape (spinbox range, sentinel value, decimals, line-edit placeholder, Browse button label), and an end-to-end nmap load confirms real tool rendering still works.

#### Full suite results
- **All 6 test suites pass: 3,227/3,227 assertions, 0 failures**
  - Functional: 2,740/2,740 (+40)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.7] — 2026-04-22

UI polish pass on the Help and View menus, plus a new User Guide dialog covering the core workflow.

### Added

- **Help → User Guide dialog** covering tool loading, presets, history, and schema creation. The new entry sits between About Scaffold and Cascade Guide in the Help menu.

### Changed

- **Keyboard Shortcuts dialog now uses a monospace layout** with grouped section headers (File & Tools, Presets & History, View, Execution), so the shortcut column lines up cleanly under each heading.

### Fixed

- **View menu items no longer have cramped spacing** between the label and the right-aligned shortcut key. A single `QMenu::item` padding rule applied to the menu bar gives every menu uniform breathing room.

- **Dark mode: menu bar items no longer shift/shrink on hover.** Adding an explicit `QMenuBar::item` base rule alongside the `:selected` rule keeps Qt from mixing stylesheet and native rendering across hover states, so the item box dimensions stay constant.

**Tests (+8 assertions):** 8 in `test_functional.py §192` (Help menu contains User Guide action; dialog opens as a visible QDialog; content mentions "schema", "preset", "history", "PROMPT.txt"; dialog closes cleanly). Existing `§98g1–g3` continue to cover the Keyboard Shortcuts dialog content. The two Fixed items are visual-only and have no new assertions.

#### Full suite results

- **All 6 test suites pass: 3,187/3,187 assertions, 0 failures**
  - Functional: 2,700/2,700 (+8)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.6a] — 2026-04-21

Small refactor release. Two unrelated dialogs in the app each carried its own copy of the same file-picker and directory-picker logic. Those duplicated copies are now a single pair of helpers shared by both dialogs.

### Changed
- **File- and directory-picker logic is now shared.** Two independent dialogs (the tool form and the cascade-variables dialog) each had its own near-identical implementation of the file and directory picker buttons. Both now call the same pair of module-level helpers, so any future change to picker behavior happens in one place instead of two.

### Tests
New §191 in `test_functional.py` guards against regression: the helpers are verified to exist at module scope with the expected signature, the two classes that previously held copies no longer define them, and no caller retains the old method-bound call shape.

#### Full suite results
- **All 6 test suites pass: 3,179/3,179 assertions, 0 failures**
  - Functional: 2,692/2,692 (+18)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.6] — 2026-04-21

Maintenance release with one small latent-bug fix. Four places in the code had near-identical logic for turning a user-supplied name into a safe filename. Three of them agreed on the rules; one didn't, and was producing slightly different (and slightly worse) filenames than the others. All four now share a single helper and follow the same rules.

### Fixed
- **Recovery filenames no longer preserve ambiguous dots.** When the app crashes mid-edit and saves recovery state, the recovery file's name is derived from the tool's name. If a tool's name contained dots (e.g., `my.tool.v2`), the recovery file previously kept those dots (`scaffold_recovery_user_my.tool.v2.json`), which is hard to read and easy to mistake for a different file extension. Dots in tool names are now replaced with underscores, matching how preset and cascade names are already saved.

### Changed
- **Filename sanitization is now handled in one place.** Previously, the logic for turning a name into a safe filename was spelled out at four different call sites (cascade save, cascade import, preset save, and recovery path). A single helper now handles all four, so any future change to the sanitization rules happens in exactly one place.

### Tests
New §190 in `test_functional.py` guards against regression: the new helper is verified to handle a range of canonical inputs correctly, the old raw regex is confirmed to appear only once in the source (inside the helper), and the recovery-file path is exercised with a dotted tool name to lock in the bug fix.

#### Full suite results
- **All 6 test suites pass: 3,161/3,161 assertions, 0 failures**
  - Functional: 2,674/2,674 (+24)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.5] — 2026-04-21

Maintenance fixes. This release tightens how the app handles unexpected errors: eight places that used to quietly swallow any error now only swallow the specific ones they're designed to handle, and one cleanup routine is reworked so it can never leave the app in a half-reset state.

### Fixed
- **Regex-pool cleanup is now guaranteed to finish.** When the background worker used for regex matching is retired, the code that clears the reference to it previously sat outside the error-handling block. If cleanup failed in an unexpected way, the app could be left holding a reference to a worker that was already gone, and the next regex match would fail. The reference-clearing step now always runs, regardless of how cleanup behaves.
- **Eight error handlers no longer swallow the wrong errors.** Several spots in the code caught "any error at all" when they were really only meant to handle a specific, predictable failure (a missing attribute, a Qt object being torn down, etc.). They now catch only the errors they're designed for, so any genuinely unexpected error will show up as an error instead of disappearing.

### Changed
- **The error handlers that must stay broad now explain why.** Eleven places legitimately need to catch any error — for example, the UX-fallback path that keeps a form rendering when a widget fails to build, or the cascade-step overlay that deliberately continues with the preset's value when an override fails. Each now carries a one-line comment explaining the reason, so future readers (and future maintainers) know the broadness is intentional.

### Tests
New §189 in `test_functional.py` guards against regression: the regex-pool cleanup is exercised with four different failure modes to confirm the reference always clears; the narrowed handlers are verified present in source; the total count of broad handlers is bounded between 9 and 11; and the explanatory-comment prefix is confirmed on the expected number of sites.

#### Full suite results
- **All 6 test suites pass: 3,137/3,137 assertions, 0 failures**
  - Functional: 2,650/2,650 (+12)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.4] — 2026-04-21

Maintenance fixes. Small drifts between related code paths closed out, a dead constant removed, and test coverage of bundled cascades made self-maintaining. No user-visible behavior change.

### Fixed
- **Cascade-example test coverage now picks up new example files automatically.** The guard that verifies bundled cascades still load iterated a hardcoded list of four filenames, so examples added later were silently excluded from coverage. It now enumerates `cascades/*.json` at runtime.
- **Empty cascade slots are now built in one place.** Four parallel code paths (add, load, import, restore) each constructed the same empty-slot dict by hand, so any future change to the slot shape had to be made in lockstep across all four. They now share a single helper.
- **Dependency wiring uses the same field-key helper as the rest of the form.** One function was building `(scope, flag)` tuples by hand while every other site in the form went through the established helper — a minor drift risk, now gone.

### Removed
- **Unused `CHAIN_FINISHED` chain-state constant.** It had no readers and was only exercised by its own "still exists" assertion.

### Tests
New §188 in `test_functional.py` guards against regression of the above: the removed constant stays absent, the field-key helper returns its canonical shape, the new slot helper is present with the expected shape, the four baseline cascade examples remain on disk, and the renumbered §187 section marker is unique. Assertion totals below.

Also: renumbered a duplicated §30 section header to §187 (two different tests carried the same number), cleaned up two stale "Phase 2" comments, and renamed placeholder single-letter exception variables to conventional names.

#### Full suite results
- **All 6 test suites pass: 3,125/3,125 assertions, 0 failures**
  - Functional: 2,638/2,638 (+7)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52

## [v2.10.3] — 2026-04-21

Stale-preset visibility and a schema-bounds validation guard. Integer and float fields used to silently clamp out-of-range preset values to the field's bounds, destroying the original intent with no feedback. Loading continues to clamp (Qt behavior unchanged) but out-of-range values now surface a stderr line so the drift is visible. Separately, tool schemas declaring integer bounds at Python's integer extremes used to silently produce a text field instead of a number field; validation now rejects these schemas with a clear error.

### Fixed

- **Out-of-range preset values silently clamped on integer/float fields** — loading a preset with a value outside the schema's declared `min`/`max` quietly clamped to the field's bound, so a preset saying `--rate 99.99` on a field capped at 10.0 would load as 10.0 with no indication the original intent was lost. Loading still clamps (Qt's behavior is unchanged), but out-of-range values now surface a `[preset]` stderr line reporting the preset value, the clamped value, and the schema range. Non-finite floats (NaN/inf) get a distinct `non-finite value` warning.

- **Tool schemas declaring integer bounds at Python's integer extremes silently produced a text field instead of a number field** — an integer argument with a declared `min` at or below -2147483647 (or `max` at or above 2147483646) would overflow Qt's 32-bit spinbox sentinel allocation and the widget factory would fall back to a plain text field with no diagnostic. Validation now rejects these schemas at schema-load time with a clear error. Float bounds are unaffected — QDoubleSpinBox handles the full Python float range.

### Added

**Tests (+15 assertions):** 9 in `test_functional.py §185` (integer/float range-clamp warning: below/above/in-range for int and float, NaN, inf, no-schema-bounds policy, sentinel-widget schema-vs-widget-bounds); 6 in `test_functional.py §186` (validate_tool rejects extreme integer min/max at Qt sentinel boundaries, accepts normal and float extremes).

#### Full suite results

- **All 6 test suites pass: 3,118/3,118 assertions, 0 failures**
  - Functional: 2,631/2,631 (+15)
  - Security: 231/231
  - Preset validation: 65/65
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.2] — 2026-04-21

Four findings from the 2026-04-20 audit, closed as a batch. No schema changes, no breaking changes, no user-facing changes for valid presets.

### Fixed

- **Preset load could crash mid-apply on corrupted meta-keys** — a preset with a wrong-type value for `_extra_flags`, `_subcommand`, or another scaffold-managed meta-key (from hand-editing, corruption, or a foreign version) was accepted by validation and then crashed inside `apply_values` when Qt tried to use it. Validation now type-checks meta-keys up front and rejects mismatches cleanly.

- **Recovery cleanup could crash on corrupted recovery files** — a recovery file with a non-numeric `_recovery_timestamp` crashed startup cleanup with a `TypeError`, blocking further recovery prompts for that session. Corrupt recovery files are now deleted silently, matching how invalid JSON is already handled.

- **Cascade slot-click didn't warn about stale presets** — clicking a cascade slot silently applied old preset values even when the tool's schema had changed since the preset was saved. The preset menu and the chain runner already warned in this case; slot-click now does too, with a non-blocking status-bar message.

- **Cascade imports accepted paths escaping the scaffold directory** — a cascade file crafted to reference `../../../etc/passwd` or similar could cause scaffold to read files outside its own directory. Relative paths are now validated to stay inside the scaffold directory; absolute paths are still accepted (consistent with File > Open), but `..` traversals are rejected with a clear error.

- **Preset load could leave a stale subcommand selection** — applying a preset that omitted or had an invalid `_subcommand` field kept whatever subcommand was previously selected instead of resetting. Now matches "Reset to Defaults" behavior and selects the first subcommand.

### Added

- **`PRESET_META_KEY_TYPES` registry** — single authoritative list of preset meta-keys and their expected types; consumed by `validate_preset`.
- **`_cascade_path_is_safe` helper** — shared traversal check used by both cascade-import paths.

**Tests (+46 assertions):** 18 in `test_preset_validation.py` (meta-key types); 12 in `test_security.py §12` (path-traversal guard); 6 in `test_security.py` (recovery timestamp guard); 4 in `test_functional.py §183` (slot-click schema-hash); 6 in `test_functional.py §184` (subcommand reset).

#### Full suite results

- **All 6 test suites pass: 3,103/3,103 assertions, 0 failures**
  - Functional: 2,616/2,616 (+10)
  - Security: 231/231 (+18)
  - Preset validation: 65/65 (+18)
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52



## [v2.10.1] — 2026-04-20

Two findings from the 2026-04-20 audit. No schema changes, no breaking changes.

### Fixed

- **Large presets were accepted from cascade but rejected from the UI picker** — the preset picker and importer gated on a 1 MB limit while cascade slot-click used a 2 MB limit, so a 1.5 MB preset would load via cascade but not via File > Open. All four preset-load paths now use the same 2 MB limit.

- **Schema validation could hang on malicious validation patterns** — a tool schema could ship a regex like `(a+)+$` that would freeze the form's real-time field validator on certain inputs. Validation patterns are now screened for catastrophic backtracking at schema-load time. All 10 patterns across shipped tools are simple and unaffected.

### Added

**Tests (+18 assertions):** 11 in `test_security.py` (ReDoS rejection + shipped-schema regression guard); 7 in `test_preset_validation.py` (preset size-gate constants).

#### Full suite results

- **All 6 test suites pass: 3,057/3,057 assertions, 0 failures**
  - Functional: 2,606/2,606
  - Security: 213/213 (+11)
  - Preset validation: 47/47 (+7)
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52


## [v2.10.0] — 2026-04-20

First batch of audit-response fixes. An external audit (7× Opus 4.7 + 7× MiniMax M2.7 agents via PaperclipAI/Hermes, each focused on a different section of the codebase) surfaced the findings driving this release and the next several. Eight findings closed across schema validation, preset handling, and shipped data. No breaking changes for valid schemas or presets. One new prompt appears for legacy presets missing a format marker — every preset Scaffold has ever saved itself carries the marker and loads silently.

### Fixed

- **Tool schemas could specify a `binary` starting with `-`** — a value like `"binary": "--user"` would be interpreted as an option to the elevation helper (gsudo/pkexec) instead of as the program to run. Leading-dash binaries are now rejected at schema-load time. Complements the v2.9.9 `--` terminator fix.

- **Tool names containing path separators silently created directories outside `presets/`** — a schema with `"tool": "../../etc"` passed validation and produced unexpected folders on first preset save. Tool names are now restricted to 1–64 characters of letters, digits, spaces, or `_.()-`, with no `..` and no leading/trailing whitespace.

- **A subcommand named `__global__` silently clobbered global fields** — the name is reserved internally to tag global-scope arguments; a schema using it as a subcommand name would overwrite the global field registry. Now rejected at schema-load time.

- **Required enum fields without a default silently picked the first choice** — a required `enum` or `multi_enum` argument with no `default` caused the form to auto-select the first item, producing a command-line value the user never chose. Schemas must now declare an explicit default for required enum fields, or mark the field optional.

- **Preset validation flagged every valid global flag as "unknown"** — when validating a preset against its tool schema, the validator's internal key format didn't match the format presets are actually saved in, so every global-scope flag looked foreign. Key formats now match; valid presets validate cleanly.

- **Cascade paths skipped preset validation entirely** — clicking a cascade slot or advancing the chain runner loaded the preset directly into the form without the structural checks the UI picker already ran. Malformed preset data could silently produce wrong commands during unattended cascade runs. All four preset-load paths now validate before applying. Interactive paths still prompt "Load anyway?" on a missing format marker; cascade paths reject outright since they run unattended.

- **Three shipped nmap presets were missing the format marker** — `full_port_scan.json`, `quick_ping_sweep.json`, and `stealth_recon.json` now carry `"_format": "scaffold_preset"` like the other 50 shipped defaults. Previously they triggered the new legacy-preset prompt on every load.

### Changed

- **Legacy presets without a format marker now prompt before loading** — previously silent. Affects hand-edited files and presets saved by pre-`_format` versions of Scaffold. Every preset Scaffold has saved itself carries the marker and loads without prompting. Cascade paths reject rather than prompt, since cascade runs are unattended.

### Added

**Tests (+49 assertions):** new coverage for the four schema-validator guards (leading-dash binary, tool-name shape, reserved subcommand name, required-enum default); a serialize → validate round-trip pinning that presets Scaffold writes itself always validate against their own schema; a cohort test walking all 53 shipped default presets; and static-scan guards ensuring every `apply_values` call site is preceded by `validate_preset`.

#### Full suite results

- **All 6 test suites pass: 3,039/3,039 assertions, 0 failures**
  - Functional: 2,606/2,606
  - Security: 202/202 (+32)
  - Preset validation: 40/40 (+17)
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52




## [v2.9.9] — 2026-04-20

Defense-in-depth for elevated commands: pkexec invocations now include the `--` options terminator between the elevation helper and the target command. Prevents a schema with `"binary": "--user"` from being parsed as a pkexec option instead of as the program to run. Pre-fix impact was at worst a misrouted command — pkexec defaults to root regardless — but closing the parser-confusion surface is cheap. Linux and macOS only; gsudo on Windows is unaffected (older gsudo versions reject `--`).

### Fixed

- **pkexec invocations now include the `--` options terminator** — `[tool] + cmd_list` is now `[tool, "--"] + cmd_list` on Linux/macOS. Belt-and-braces alongside input validation, not a replacement for it.

### Added

**Tests (+2 assertions):** §102f split into platform-gated cases (pkexec with `--`, gsudo without) plus two new guards pinning the terminator position (exactly one `--`, immediately after the tool).

#### Full suite results

- **All 6 test suites pass: 2,990/2,990 assertions, 0 failures**
  - Functional: 2,606/2,606 (+2)
  - Security: 170/170
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.8] — 2026-04-19

Defensive hardening of `normalize_tool` against malformed input, plus a seeded schema-fuzzing section in the security suite. 300-iteration fuzzing found crashes on malformed inputs that production code paths never actually produced (`validate_tool` catches them first), but the independent guard closes the gap for any future caller.

### Fixed

- **`normalize_tool` crashed on malformed schemas** — feeding it a schema with non-list `arguments` or non-dict entries raised `AttributeError` or `TypeError` instead of returning a normalized dict. Now skips malformed shapes rather than crashing. A non-dict passed as the top-level argument still raises `TypeError` explicitly, since that's a programmer error.

### Added

**Tests (+11 assertions):** new `test_security.py §11` — 300 seeded schema mutations across 9 categories (type swaps, key corruption, deep nesting, etc.) pinning that `validate_tool` never raises and `normalize_tool` either returns a dict or raises `TypeError`.

#### Full suite results

- **All 6 test suites pass: 2,988/2,988 assertions, 0 failures**
  - Functional: 2,604/2,604
  - Security: 170/170 (+11)
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.7] — 2026-04-18

Cascade-side preset loading now validates format markers before applying, closing the `_format` / `_tool` equivalent of the schema-hash gap closed in v2.9.3.

### Fixed

- **Cascade slot click silently accepted malformed presets** — clicking a cascade slot whose preset was missing `_format`, was actually a cascade file, or belonged to a different tool would populate the form from whatever keys happened to match current-tool flags. The slot-click path now rejects these with a status-bar message naming the specific problem.

- **Cascade chain runner silently accepted malformed presets** — same failure mode during unattended chain runs: a malformed preset at step N would proceed anyway. The chain now halts with a clear error naming the file and step number.

### Added

**Tests (+12 assertions):** new `§182` covers all five paths — valid markers (regression guard), missing `_format` / tool mismatch on slot click, and missing `_format` / cascade-file-as-preset on chain advance. Each negative case pins the status-bar text, the stderr forensic line, and for chain-advance cases, that the chain actually halted.

#### Full suite results

- **All 6 test suites pass: 2,977/2,977 assertions, 0 failures**
  - Functional: 2,604/2,604 (+12)
  - Security: 159/159
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.6] — 2026-04-18

Docs-and-tests release — no code changes. Mid-token `{capture}` substitution (e.g. `--output={capture}.log`) pinned as supported behavior, and two v2.9.4 changelog bullets clarified in place.

### Added

**Tests (+8 assertions):** `§179 T17` covers four canonical mid-token patterns — `prefix-{capture}-suffix`, `{capture}.log`, `--output={capture}`, `{host}:{port}` — guarding them from silent breakage during future refactors.

### Changed

- **v2.9.4 changelog clarified in place** — the `_read_user_json` and `extra_flags {capture}` bullets now more accurately describe the enforcement semantics.

#### Full suite results

- **All 6 test suites pass: 2,965/2,965 assertions, 0 failures**
  - Functional: 2,592/2,592 (+8)
  - Security: 159/159
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.5] — 2026-04-18

Test-infrastructure release — no code changes. Adds shared context-manager helpers for common test patches, a regression guard for the multi-subcommand cascade corner flagged in v2.9.4, and size-cap coverage at three previously-uncovered JSON-load sites.

### Added

**Tests (+19 assertions):**
- New `_patch_qmb` / `_patch_stderr` context managers to reduce restore-surface-area in cascade tests.
- `§180` pins subcommand and elevation behavior on consecutive same-tool cascade steps — the corner v2.9.4 flagged as untested.
- `§181` covers preset file-size rejection at three additional sites (cascade list load, preset description edit, cascade slot click).

#### Full suite results

- **All 6 test suites pass: 2,957/2,957 assertions, 0 failures**
  - Functional: 2,584/2,584 (+19)
  - Security: 159/159
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.4] — 2026-04-18

Cascade resilience pass. Closes three silent-failure modes in the cascade pipeline (wrapper exceptions, subprocess-pool creation failures, literal `{capture}` tokens in the Additional Flags buffer), adds size caps on every JSON-load path, surfaces QSettings corruption instead of silently resetting, and fixes a snapshot-overlay bug where consecutive same-tool cascade steps clobbered each other's presets.

### Fixed

- **Cascade wrapper exceptions no longer lock up the UI** — an uncaught exception from a cascade signal handler used to leave the chain mid-step with inconsistent buttons and no way to recover short of restart. The chain now returns to idle cleanly and names the failing step in the status bar.

- **Subprocess pool creation failures now halt the cascade cleanly** — on resource exhaustion or fork/spawn denial, the cascade used to crash silently. It now halts with a `pool_error` capture-error entry.

- **Autosave failures now notify you on first occurrence** — previously they logged only to stderr and you discovered the loss on next launch. One-shot status-bar warning per session; further failures stay silent to avoid noise.

- **Corrupted cascade/variables settings now surface a warning instead of silently resetting** — if the QSettings JSON for cascade slots, cascade variables, or custom paths is corrupt, the affected key is named in the warning so you can restore from backup before re-saving overwrites recoverable state.

- **Oversized preset and cascade files are now rejected at every load site** — three cascade-side JSON-load paths (cascade import, cascade list, cascade-step preset resolution) now enforce the 2 MB preset cap via a new `_read_user_json` helper. The two preset-side paths continue to enforce the 1 MB schema cap.

- **Consecutive same-tool cascade steps no longer clobber each other's presets** — the snapshot overlay used to replace step N's preset wholesale with step N-1's form state. Now merges: the snapshot only wins for keys it actually contains.

- **Additional Flags buffer now substitutes `{capture}` tokens in cascade runs** — the buffer was being copied to argv verbatim, so `{name}` reached the subprocess as literal braces. Now routed through the normal capture-substitution pipeline. Each `{capture}` must resolve to a single shell token; multi-token values halt the chain. Mid-token references like `prefix-{capture}-suffix` or `--output={capture}` are supported.

### Changed

- **Capture-error halt semantics tightened** — pool-creation errors now halt unconditionally; regex errors, timeouts, and worker crashes halt only when `stop_on_error` is on. Previously all four silently logged a warning and kept going.

- **`extract_captures` return shape changed** — error tuples now carry an explicit error-type field. Internal only; no public API change.

- **Multi-subcommand same-tool cascades: subcommand and elevation now follow the step N preset** — consequence of the overlay-merge fix. No existing test exercises this corner, so it's untested for now — file an issue if you hit a problem.

### Added

**Code:**
- `_read_user_json` helper and `MAX_PRESET_SIZE` constant for the 2 MB preset cap.
- `_substitute_in_extra_flags` for capture substitution in the Additional Flags buffer.

**Tests (+61 assertions):** new `§179` T1–T16 covers every fix above — wrapper recovery, pool failure, autosave notification, QSettings corruption surfaces, size-cap rejection, capture substitution (single-token, multi-token, unset, literal-brace passthrough), capture-error halt asymmetry, and snapshot overlay merge behavior.

#### Full suite results

- **All 6 test suites pass: 2,938/2,938 assertions, 0 failures**
  - Functional: 2,565/2,565 (+61)
  - Security: 159/159
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.3] — 2026-04-17

Completes the cascade stale-preset hardening arc deferred from an earlier release.

### Fixed

- **Cascade runs halt on preset / schema mismatch instead of running the wrong command** — if a tool schema changed after a preset was saved (e.g. a flag renamed), the cascade used to silently drop the renamed key, build an incomplete command, and run it against the live binary with no warning. The cascade now halts with a status-bar message naming the preset and the affected step, telling you to re-save against the current schema. The interactive preset picker already warned in this case — the fix brings the cascade runner up to parity. Legacy presets without a schema hash load silently, matching the interactive path.

### Added

**Tests (+14 assertions):** new `§178` covers all three cases — schema changed after save (halts), schema unchanged (runs normally), legacy preset without hash (backward compat).

#### Full suite results

- **All 6 test suites pass: 2,877/2,877 assertions, 0 failures**
  - Functional: 2,504/2,504 (+14)
  - Security: 159/159
  - Smoke: 78/78
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.2] — 2026-04-17

Cascade sidebar button fix on Linux, a new `_tool` tag on saved presets, a pre-flight dependency check before a cascade runs, six new bundled tool schemas with default preset packs, and two new example cascades.

### Added

- **Presets now remember which tool they belong to** — saved presets carry a `_tool` tag. Loading a preset that belongs to a different tool than the one currently open shows a warning dialog naming both tools, with "Load Anyway" and "Cancel" options. Presets saved by older versions of Scaffold have no tag and load silently (backward compat).

- **Cascade dependency pre-flight check** — before importing or loading a cascade, Scaffold now checks that every tool schema and preset it references actually exists on disk. Missing files are listed in a dialog with "Continue" / "Cancel" — Cancel aborts cleanly without touching the sidebar.

- **Six new bundled tool schemas** — `find`, `ncat`, `wget`, `ssh` (37 args), `scp` (21 args), `tar` (57 args). `ncat` covers Nmap's netcat. `find` and `wget` are partial coverage with niche flags omitted, documented per file. Bundled tool count now 28.

- **25 new default presets** across the new tools: `find` (6), `ncat` (5), `wget` (5), `ssh` (5), `scp` (4), `tar` (5).

- **Two new example cascades** — `cascade_nmap_ncat` (nmap discovers open ports, ncat connects on each one via a shared `HOST` variable) and `cascade_ssh_scp` (ssh verifies a remote file exists, scp downloads it with halt-on-error).

### Fixed

- **Cascade sidebar button text no longer clips on Linux** — four of the seven chain-row buttons weren't picking up the compact padding that the other three used, so they overflowed the fixed width budget on Linux light mode. All seven buttons now share one source of truth for their styling.

- **Toggling a cascade button off no longer wipes its padding** — the "off" state for Loop and Stop-on-Error used to clear the stylesheet entirely, erasing the compact padding on every cascade load.

### Changed

- **Cascade button styling consolidated** — four duplicated style fragments replaced with a single variable; theme changes now refresh every chain-row button instead of only a subset.

**Tests (+72 assertions):**
- `§174` — cascade chain-button padding regression guard across init, toggles, and theme flips (32 assertions).
- `§175` — bundled cascades intact guard, catches any future test that forgets to redirect the cascades directory (4 assertions).
- `§176` — `_tool` tag coverage: serialization, matching/mismatched load, Cancel vs Load Anyway, backward compat for untagged presets (12 assertions).
- `§177` — cascade dependency pre-flight: all-present, missing-tool, missing-preset, mixed, and empty cases through both import and load paths (24 assertions).

#### Full suite results

- **All 6 test suites pass: 2,863/2,863 assertions, 0 failures**
  - Functional: 2,490/2,490 (+82)
  - Security: 159/159
  - Smoke: 78/78 (+8)
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9.1] — 2026-04-16

POSIX shell-quoting fix for the command-preview copy format.

### Fixed

- **Command preview copy now produces valid POSIX shell syntax** — tokens containing both whitespace and a single quote (e.g. `it's a test`) were being escaped with Windows CMD-style `""` syntax, which is invalid in any POSIX shell. Now uses standard POSIX single-quote escaping that round-trips cleanly. Only affected the "generic" copy format; the Bash-specific format was already correct.

### Added

**Tests (+11 assertions):** new `§173` pins POSIX correctness and round-trip behavior. `§92` updated to reflect correct POSIX output.

#### Full suite results

- **All 6 test suites pass: 2,773/2,773 assertions, 0 failures**
  - Functional: 2,408/2,408 (+11)
  - Security: 159/159
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.9] — 2026-04-16

Major release. **Custom PATH support** — Scaffold can now find tool binaries in locations beyond your system PATH. Every launch scans your system PATH plus any extra directories you've configured, and matches installed binaries to the tool schemas Scaffold knows about. Also ships a full regex-safety pipeline for cascade captures (invalid patterns caught at save, runtime timeouts on catastrophic backtracking) and closes three bugs from a Claude Opus 4.7 code review.

### Added

- **Custom PATH configuration** — if your binaries live outside the system PATH (Homebrew Cellar, custom install directories, unpacked release tarballs), you can now add those directories to Scaffold's search path. The tool picker will find them on next launch.

- **Invalid regex patterns are now caught when you save** — previously, a broken regex in a cascade capture (e.g. `[unclosed`, `(?P<`) passed validation and was silently discarded at runtime, leaving you with no feedback about why the capture never fired. Save and import now compile the pattern and reject it with a clear error.

- **Runtime regex errors now report distinctly** — a capture that errors at runtime is now reported through a separate channel from captures that simply didn't match, so you can tell "my regex is broken" from "my regex didn't match anything."

- **Regex timeout protection (ReDoS hardening)** — a pathological pattern like `^(a+)+$` against 64 KB of input used to be able to freeze the UI thread indefinitely during cascade execution (measured ~20 seconds at 30 input chars, astronomical at 64 KB). Two-layer defense now:
  - **At save time** — a heuristic rejects the classic catastrophic-backtracking shapes (nested quantifiers, alternation under a quantifier). 100% catch rate on the four classic ReDoS patterns, zero false positives across fourteen realistic patterns from existing tool schemas.
  - **At runtime** — regex matching runs in a subprocess worker with a 2-second wall-clock timeout. On timeout the worker is killed and replaced, and the capture reports as errored. Uses a subprocess rather than a thread because the Python regex engine doesn't release the GIL, so a thread can't be interrupted.

### Fixed

(Three bugs identified in a Claude Opus 4.7 code review — see Added above for the full list; every Added item was driven by a review finding.)

### Changed

- **Capture-extraction return shape changed** — now returns `(captures, errors)` instead of just `captures`. Internal callers updated; external callers need to unpack the tuple.

- **New stdlib imports** — scaffold now imports `multiprocessing` and `threading` from the standard library. No new third-party dependencies.

### Docs

- **README: "Using tools that aren't on your PATH"** — new subsection explaining that `binary` accepts a bare name (PATH-resolved) or an absolute path, why shell-relative paths like `./installer` don't work (Scaffold doesn't invoke a shell), and the two ways to fix it. Walkthrough uses a rayhunter installer as the example.

- **SCHEMA_PROMPT.txt rule 17** — nudges the LLM to flag tools that ship as downloaded release binaries, since those users may need to configure a custom PATH or set an absolute `binary`.

**Tests (+70 assertions):**
- `§167` — invalid regex rejection at save (24 assertions).
- `§168` — runtime regex errors surface through the errors channel (10 assertions).
- `§170` — save-time heuristic rejects ReDoS-prone patterns, accepts realistic ones (22 assertions).
- `§171` — cascade runs time out on ReDoS patterns within budget (3 assertions).
- `§172` — heuristic unit tests, prone and safe patterns (22 assertions — split across the new section and supporting coverage).

#### Full suite results

- **Functional: 2,397/2,397 assertions, 0 failures** (+70 across §167, §168, §170, §171, §172)
- Other suites unchanged.


## [v2.8.7.3] — 2026-04-16

Error-path audit. Two real bugs fixed, three defensive tightenings.

### Fixed

- **Cascade no longer runs the wrong tool when a step's tool file is missing** — if the tool file for step N had been deleted or moved, the cascade would show a "Load Error" dialog and then silently execute the *previous* step's binary with stale form data. The chain now halts cleanly with a clear status message instead.

- **Cascade halts when a step's preset file is missing** — if a configured preset file no longer existed on disk, the step used to silently run with default values instead of the expected configuration. This is treated as a configuration error (unconditional halt) rather than a runtime error, so it halts regardless of the stop-on-error setting.

- **Exporting a preset with malformed JSON no longer crashes silently** — if the source preset file contained invalid JSON, export used to raise an unhandled exception with no user feedback. Now surfaces a clear error.

### Changed

- **Recovery file discard now surfaces a message** — when a recovery file is found but the referenced tool has been moved, the file is still discarded (same behavior as before) but the status bar now tells you why: "Recovery file discarded — tool location has changed."

- **Corrupt cascade history entries no longer crash the history dialog** — corrupt QSettings data containing non-dict elements used to crash the History dialog on open. Non-dict entries are now filtered out during load.

### Added

**Tests (+regression guards):**
- `§162` — cascade tool-deleted-mid-run regression test: 2-slot cascade, delete the second tool file, run the chain. Asserts halt, exactly 1 step recorded, wrong tool never loaded.
- `§163` — cascade missing-preset halt test: 1-slot cascade, delete the preset file, run the chain. Asserts halt, 0 steps executed, status bar message names the missing file.

#### Full suite results

- **All 6 test suites pass: 2,594/2,594 assertions, 0 failures**
  - Functional: 2,229/2,229
  - Security: 159/159
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.7.2] — 2026-04-16

Test coverage catch-up for v2.8.7.

### Added

**Tests (+4 sections):** regression guards pinning the atomic-write cascade path, capture reserved-name membership, default-No destructive confirmations, and the documented backslash carve-out (accepted in binary paths, rejected in subcommand names).


## [v2.8.7.1] — 2026-04-16

Post-v2.8.7 polish. Crash-safety gap closed in cascade save/import, a confirmation-dialog default tightened, and some dead defensive code dropped.

### Fixed

- **Cascade save and import are now crash-safe** — both used to write JSON with a plain overwrite, which could leave the file truncated or empty if the process was killed or the disk filled mid-write. Now atomic, matching every other JSON writer in the app. Cascade save is the higher-value fix — a user actively naming a cascade expects it to survive a crash.

### Changed

- **Clear Cascade History confirmation now defaults to No** — matches the Delete confirmation added in v2.8.7. Other confirmation dialogs pre-date this pattern and are intentionally left alone.

- **Dropped legacy entries from the capture reserved-name list** — `stdout_full` and `stderr_full` are rewritten to their new names during cascade load (see v2.8.7 rename), so keeping them as reserved names served no purpose. The migration code itself still handles both.

### Added

**Tests (+regression guard):** `§158` locks in the stop-during-delay guard — a stop during a cascade's between-step delay should record exactly the steps that actually ran, with no phantom "stopped" entry for the step that never started.

### Docs

- **Two silent traps commented in place** — the backslash carve-out in binary-path validation (Windows paths need it; subcommand names don't) and the cascade history version-bump contract (bumping the version silently drops all existing history unless a migration is added). No code changes, just breadcrumbs for future edits.

#### Full suite results

- **All 6 test suites pass: 2,574/2,574 assertions, 0 failures**
  - Functional: 2,210/2,210
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.7] — 2026-04-15

Cascade history hardening, capture-source rename with silent migration, stop-mid-step recovery, and UI polish — closes the v2.8.6 code review.

### Added

- **Delete confirmation in cascade history** — deleting a cascade history entry now prompts first. If you select a step within a cascade rather than the cascade itself, the message makes the scope explicit ("Deleting this step will remove the entire parent cascade entry from history. Continue?"). Default button is No.

- **Tool picker theme refresh** — folder-header rows now update their backgrounds when you switch themes, without touching your selection, scroll position, or active filter.

### Changed

- **Cascade captures renamed: `stdout_full` → `stdout_tail`, `stderr_full` → `stderr_tail`** — the old names implied the entire stream was captured, but the implementation has always sliced to the last 64 KB. The new names match reality. Existing cascade files and history entries are migrated silently on load — no user action needed.

### Fixed

- **Cascade history loads cleanly if the version key is corrupt** — a non-numeric version value (from a corrupted registry or manual edit) used to propagate an exception up the load path. Now returns an empty history, matching how corrupt JSON is already handled.

- **Stopping mid-step now preserves the in-progress step in history** — stopping during step N used to produce a history entry showing only N−1 steps. The stopped step is now recorded with `error: "stopped"` before the cascade terminates.

- **No more spurious writes when updating a missing cascade run** — calling the update helper with an unknown run ID used to issue a QSettings write regardless. Now only writes if a matching entry was actually found.

- **File-source captures require an explicit `path` key** — the validator has always required it, but the runtime used to fall back to the `pattern` key if `path` was missing. Both paths now require `path`, eliminating a subtle divergence where a capture could seem valid in one context and invalid in another.

### Security

- **Shell metacharacter screen extended** — `#`, `'`, and `"` are now rejected across the board; `\` is rejected in subcommand names but still allowed in binary paths (needed for Windows absolute paths like `C:\Windows\System32\ping.exe`). Defense-in-depth — none of these were exploitable since Scaffold never invokes a shell, but they shouldn't appear in executable identifiers either.

### Added

**Tests (+6 sections):** corrupt history version key, no-op update, stop-mid-step step recording, tool picker theme refresh, legacy capture-name migration, delete-confirmation respects user choice.

#### Full suite results

- **All 6 test suites pass: 2,569/2,569 assertions, 0 failures**
  - Functional: 2,205/2,205
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.6] — 2026-04-15

New Cascade History menu, an experimental LLM-powered cascade generation guide, and output panel resize fixes.

### Added

- **Cascade History** — record and browse past cascade runs, restore individual steps or full cascades, export history to JSON. Documented in the README, this changelog, and the in-app Cascade Guide.

- **Cascade generation guide (experimental)** — new `CASCADE_GENERATION_GUIDE.md` at the repo root documenting a workflow for generating cascade files via an LLM with persistent access to your `tools/` and `presets/` directories (e.g. a Claude Project). Split into a setup half for humans and an instructions half to paste into the LLM's custom instructions. Preset-first generation with schema fallback. Docs only — no code changes. Feedback and edge-case reports welcome.

### Fixed

- **Output panel drag handle no longer caps at ~140 pixels** — the measurement logic had a circular dependency with the form scroll area, which would inflate to fill any space the output panel gave up. The calculation now uses intrinsic widget sizes, so the output panel can grow to its actual available space.

- **Output panel scrollbar no longer jumps during drag resize** — if you were scrolled to the bottom, the view now stays at the bottom as you drag. Otherwise the scroll offset is preserved.

### Changed

- **Output panel height is now dynamic** — removed the static 800-pixel ceiling. On large windows the output panel can grow to fill its full available space.

#### Full suite results

- **All 6 test suites pass: 2,553/2,553 assertions, 0 failures from changes**
  - Functional: 2,188/2,189 (1 pre-existing flake in §123b, unrelated)
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.11] — 2026-04-14

History crash/error reporting, test stabilization.

### Added

- **History now distinguishes crashes from normal exits** — the Exit Code column shows descriptive labels ("crashed", "not found", "timed out", "I/O error") with a tooltip for the specific error type, instead of a raw numeric code.

### Fixed

- **Flaky §26h geometry assertion stabilized** — was failing intermittently (~1 in 8) due to a window-manager timing race. Added a wait step and widened the pixel slack.

#### Full suite results

- **All 6 test suites pass: 2,260/2,260 assertions, 0 failures**
  - Functional: 1,896/1,896
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.10] — 2026-04-13

Cascade button polish, variable-dialog column clamping, history empty-state.

### Fixed

- **Cascade Run button now says "Running..." during execution** — used to display "Run..." mid-run.

- **Cascade buttons have tooltips** — Run, Stop, and Clear were missing tooltips entirely. All six cascade buttons now have them.

- **Cascade variable dialog columns no longer draggable off-screen** — matches the clamping pattern used everywhere else in the app.

- **History entry guard catches missing capture variables** — the recorder used to check for only one of three required variables, risking an attribute error if the other two were missing.

- **History capture variables set before signal connections** — in the rare case where a QProcess failed to start synchronously, the error handler could fire before the capture variables were created.

### Improved

- **History empty-state now shows a message** — used to display empty table headers when history was empty. Now shows a centered message and disables the Clear History button until there are entries.

### Added

**Tests:** cascade variable dialog column clamping (`§145`), history entry guard with missing capture vars, empty-state UX.

#### Full suite results

- **Functional: 1,896/1,896**


## [v2.8.5.9] — 2026-04-13

Audit backlog cleanup — closes the v2.7.4-era audit cycle. Two small fixes, one dead-code removal.

### Fixed

- **Cascade field substitution no longer leaves the form in a half-written state on halt** — if stop-on-error fired partway through substituting capture values into form fields, the fields processed before the halt had already been written while later fields hadn't. The substitution pass now collects all writes first and commits them atomically.

- **Unset-capture warnings are now deduplicated per cascade step** — if three fields in the same step referenced the same missing capture, the log used to show three identical warnings. Now shows one per capture name per step.

- **Removed a dead autosave call in the recovery flow** — after accepting a recovery prompt, a subsequent autosave call was always a no-op because the baseline had just been refreshed. Removed.

### Added

**Tests:** partial-mutation regression guard, warning dedup, recovery-flow dead-call verification.

#### Full suite results

- **Functional: 1,836/1,836** (+8)


## [v2.8.5.8] — 2026-04-13

Atomic preset writes, cascade signal handler stacking, dock visibility persistence.

### Fixed

- **Preset import and export are now crash-safe** — both still used a plain overwrite, bypassing the atomic-write helper introduced in v2.8.5.3. A disk-full or mid-write crash could corrupt the destination file. Both now write atomically.

- **Cascade signal handlers no longer stack across steps** — each cascade step used to install a wrapper around the process-finished handler without restoring it first, so after N steps there were N nested wrappers. The Run button could get re-disabled multiple times per process exit. Each step now installs against the real handler.

- **Cascade dock visibility persists when closed via the X button** — previously only the Ctrl+G / menu toggle saved the state, so closing the dock via its native X was forgotten on next launch.

### Added

**Tests:** atomic preset import regression guard, wrapper-stacking fix, visibility persistence.

#### Full suite results

- **All 6 test suites pass: 2,192/2,192 assertions, 0 failures**
  - Functional: 1,828/1,828
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.7] — 2026-04-13

Drag-enter cursor fix, preset prompt docs update.

### Fixed

- **Drag-enter cursor no longer gets stuck in "accept" state for non-JSON files** — the handler used to return silently for non-`.json` drags, which on some Qt versions/platforms left the cursor in accept mode for files Scaffold wouldn't actually load. Now explicitly rejects.

### Changed

- **Preset prompt updated for multi-preset generation** — the multi-preset section now instructs the LLM to return each preset as a separate JSON object with named delimiters, instead of a JSON array. Matches the validator's one-file-one-object requirement.

### Added

**Tests:** drag-enter rejection cases (non-.json, no URLs, mixed, case variations), preset prompt smoke test.

#### Full suite results

- **All 6 test suites pass: 2,187/2,187 assertions, 0 failures**
  - Functional: 1,823/1,823
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.6] — 2026-04-13

Import hardening — format check on preset import, malformed cascade handling.

### Fixed

- **Import Preset rejects cascade and non-preset files** — used to pass validation and silently import, then fail on next load. Now mirrors the tool-load import check: cascade files are rejected with a message pointing to the correct menu; unmarked files prompt before loading; other formats are rejected outright.

- **Cascade list dialog handles malformed `steps` field** — if `steps` was a dict or string instead of a list, the Steps column used to show a misleading count (dict length, string length, etc.). Malformed rows now show `"0 (malformed)"` so you can find and delete them, but stay visible.

### Added

**Tests:** duplicate-flag single-reporting regression guard (`§133`) — each flag collision should be reported exactly once per scope.

#### Full suite results

- **All 6 test suites pass: 2,176/2,176 assertions, 0 failures**
  - Functional: 1,812/1,812
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.5] — 2026-04-13

Cascade dialog hardening — combined error messages, duplicate flag rejection, flag-format validation.

### Fixed

- **Cascade capture dialog now shows one combined error message** — used to pop up one warning dialog per invalid row, so ten bad rows meant ten dialogs. Now collects everything into a single list. Red cell highlighting per row is preserved.

- **Cascade variable dialog rejects duplicate flags** — two variables targeting the same flag used to pass edit-time validation and silently have the second one ignored at runtime. Now rejected at edit time with a combined error.

### Changed

- **Cascade variable dialog validates flag format** — flags must now match a strict pattern (short flags like `-f`, long flags like `--output-file`, positional names like `TARGET`, or subcommand-scoped variants like `clone:--depth`). Garbage strings, bare dashes, and malformed subcommand prefixes are rejected with a combined error.

#### Full suite results

- **All 6 test suites pass: 2,149/2,149 assertions, 0 failures**
  - Functional: 1,785/1,785
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.4] — 2026-04-13

Cascade dock min-width fix and password clipboard safety.

### Fixed

- **Cascade dock min-width restored on native close** — closing the cascade sidebar via its X button used to leave the window minimum width inflated (set while the dock was open), preventing the window from shrinking back. Now syncs the min-width whichever way the dock is closed.

### Security

- **Copy Command prompts before putting passwords on the clipboard** — all four copy paths (Copy, Copy as Bash, Copy as PowerShell, Copy as CMD) now detect filled password fields and ask whether to mask them with `********` first. The default is to mask. Your choice is remembered for the rest of the session (in-memory only, never persisted) and resets when you load a new tool. Defense-in-depth — the display preview and history already masked passwords; this closes the last path where plaintext could reach the system clipboard.

#### Full suite results

- **All 6 test suites pass: 2,065/2,065 assertions, 0 failures**
  - Functional: 1,701/1,701
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.3] — 2026-04-13

Capture substitution coverage, atomic preset writes, preset picker init.

### Fixed

- **Cascade captures now work for every field type** — `{capture}` substitution used to only cover text and dropdown fields. File, directory, password, and multi-select fields were silently skipped. All field types now substitute correctly, including each item in multi-select lists.

- **Preset saves and the autosave recovery file are now crash-safe** — all three write paths now go through an atomic-write helper that writes to a temporary file and renames it into place, preventing corruption on mid-write crashes.

- **Preset picker no longer relies on a fallback for an unset attribute** — cleanup item, no user-visible change.

#### Full suite results

- **All 6 test suites pass: 2,038/2,038 assertions, 0 failures**
  - Functional: 1,674/1,674
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.2] — 2026-04-13

Five targeted fixes from an audit pass.

### Fixed

- **Enum fields honor their schema default when a preset omits them** — used to fall back to the first item in the list. Now restores the declared default.

- **Duplicate flag validation no longer double-reports** — removed a redundant second validation pass; each duplicate is now reported exactly once.

- **Schema validator emits a distinct error for whitespace in `binary`** — previously the space was lumped into a generic shell-metacharacter error, which was confusing because the space was invisible in the message.

- **Crashes no longer produce duplicate status messages and history entries** — the crash handler and the finished handler used to both fire on a crash. The finished handler now skips its reporting when the crash handler already ran.

- **Password fields preserve trailing whitespace** — pasted hex tokens and secrets with trailing spaces used to be silently stripped. No longer.

#### Full suite results

- **All 6 test suites pass: 2,018/2,018 assertions, 0 failures**
  - Functional: 1,654/1,654
  - Security: 158/158
  - Smoke: 70/70
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.5.1] — 2026-04-13

Argv-flag injection guard for cascade captures.

### Security

- **Cascade captures now warn when a captured value looks like a command-line flag** — if a captured value is something like `--output`, `-r`, or similar and it replaces a whole form field, the output panel emits a warning and the cascade halts under stop-on-error before running the next step. Bare `-`, bare `--`, negative numbers, and captured values embedded in larger strings (like `prefix-{name}-suffix`) are exempt. Defense-in-depth — Scaffold's argv-list execution already prevents shell injection, but this catches cases where a captured value could change how downstream arguments are parsed.

### Added

- **Cascade variable options show tooltips on hover** — descriptions now surface in the Edit Variables dialog.


## [v2.8.5] — 2026-04-13

Cascade captures — extract values from one step's output and feed them into later steps.

### Added

- **Cascade captures** — cascade slots can now define named captures that pull values from a step's output and inject them into later steps as `{name}` substitutions. Capture sources include regex matches on stdout/stderr, file contents by path, exit codes, and full stream tails. Captures are forward-only and share the cascade variable namespace; collisions silently take the later value. Unresolved captures emit a warning and halt the chain under stop-on-error.

- **Capture definition dialog** — new "Captures..." button on each cascade slot opens a per-step editor for defining what to extract. Source-type tooltips explain each option.

- **Example cascade: `cascades/nmap_followup_chain.json`** — nmap host discovery captures an open port, then feeds it into a targeted service scan.


## [v2.8.4] — 2026-04-11

History dialog filter bar, stronger schema hashing, cascade crash recovery guard.

### Added

- **History dialog search bar** — filter commands with case-insensitive substring matching. Escape clears the filter without closing the dialog.

- **New test coverage (10 sections, `§105`–`§114`)** — malicious cascade variable values, `apply_to: "none"` scope, cascade process-failed-to-start cleanup, recovery-file expiry and corrupt-JSON handling, binary validation relative-path rejection, empty subcommand name rejection, single-slot cascade execution, cascade crash recovery, schema-hash verification, history filter bar.

### Changed

- **Schema fingerprint now uses SHA-256** — replaces MD5 for preset version hashing.

### Fixed

- **Cascade no longer ends up unrecoverable after a crash in the step handler** — an exception in the cascade's process-finished handler used to leave the app in a state where the cascade couldn't be stopped or restarted. Now wrapped in try/except with proper cleanup.

- **Stabilized §3c stop-process test on Windows** — was flaking due to a race where the process exited before the stop button was pressed. Test now uses a ping count that ensures the process is still running.

- **Stabilized §26i drag-handle test** — was flaking on layout timing. Added synchronization before measurement.

#### Full suite results

- **All 6 test suites pass: 1,957/1,957 assertions, 0 failures**
  - Functional: 1,594/1,594
  - Security: 158/158
  - Smoke: 69/69
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.3] — 2026-04-09

Elevation helper test coverage, cascade examples, rsync tool schema, UX tweak.

### Added

- **Elevation helper test coverage** — new section pinning tool detection, caching, platform-dependent error messages, command wrapping, and label text for the elevation system.

- **Two example cascades** — `cascades/nmap_basic_chain.json` (basic two-step nmap) and `cascades/ping_then_nmap.json` (ping discovery followed by nmap with per-step flag injection via cascade variables).

- **rsync tool schema and 6 bundled presets** — 129-argument schema covering local backup, remote SSH push, dry-run mirror, incremental snapshot, move files, and selective copy with excludes. Mostly untested in the wild — bug reports welcome.

### Changed

- **Delete Tool confirmation now defaults to No** — also uses a shorter, clearer message.

- **Cascade variable injection now documented in-code** — comment block explains that cascade variables match by flag name and inject into only the first matching field.

#### Full suite results

- **All 6 test suites pass: 1,889/1,889 assertions, 0 failures**
  - Functional: 1,527/1,527
  - Security: 158/158
  - Smoke: 68/68
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.2] — 2026-04-08

Cascade import truncation, BOM-prefixed JSON handling, chain cleanup, timer leak, ghost widgets.

### Fixed

- **Cascade imports clamp to the max slot count** — importing a cascade with more than 20 steps used to crash or behave unpredictably. Now truncates to 20 and shows a status message saying so.

- **BOM-prefixed JSON files now load correctly** — Windows tools like Notepad and PowerShell can write UTF-8 files with a byte-order mark prefix, which used to cause a JSON parse error. Twelve user-facing file loads now strip the BOM before parsing.

- **Run button re-evaluates after a cascade finishes** — the button used to stay disabled if the last cascade step left required fields empty, even after cleanup. Now re-checks button state at the end of cleanup.

- **Timer leak in error handler fixed** — two internal timers used to keep running after a process failed to start. Now stopped on error, matching the normal-exit path.

- **Ghost widgets no longer briefly appear when switching tools on Linux** — widget teardown now explicitly hides and detaches widgets before deletion, avoiding a timing window where stale widgets would flash on screen.

#### Full suite results

- **All 6 test suites pass: 1,869/1,869 assertions, 0 failures**
  - Functional: 1,507/1,507
  - Security: 158/158
  - Smoke: 68/68
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.1] — 2026-04-08

Cascade variable editor rework, smarter output panel sizing, keyboard shortcuts, Cascade Guide.

### Added

- **Enter / Shift+Enter run shortcuts** — Enter runs the current command, Shift+Enter starts the cascade. Both suppressed when focus is in a text input, so typing is never hijacked.

- **Cascade Guide** — new Help menu entry with a plain-language walkthrough of cascade features: slots, variables, loop mode, pause/resume, stop-on-error.

- **"None" scope for cascade variables** — disables injection while keeping the variable defined for later use.

### Fixed

- **Cascade imports reject invalid apply-to scope values** — non-string, non-list values used to crash or be silently accepted. Now fall back to "all" with a warning.

- **Cascade variable "Apply To" selector is now a checkbox dropdown** — replaces the old free-text combo box. Prevents typos and invalid slot references.

- **Cascade variable editor columns are resizable** — with a flag column bounded between 80–280 px and the apply-to column stretching to fill remaining space.

- **Output panel drag limit no longer overlaps the form** — the drag handle used to use a fixed half-window cap that didn't account for actual sibling widget sizes. Now computes available height from actual geometry.

#### Full suite results

- **All 6 test suites pass: 1,845/1,845 assertions, 0 failures**
  - Functional: 1,483/1,483
  - Security: 158/158
  - Smoke: 68/68
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23


## [v2.8.0] — 2026-04-07

Major release. **Cascade chaining** — sequence multiple tool runs into automated workflows with shared state between steps. **LLM-powered preset generation** via a new CLI flag. Also ships a license change and a broad hardening pass across validation, security, and the UI.

### Added

- **Cascade chaining** — a new right-side dock panel (Ctrl+G) for chaining tool runs into sequential workflows. Each cascade holds up to 20 numbered slots, each assignable to a tool and optional preset. Per-step delays up to an hour, loop mode (repeat the full chain), and stop-on-error mode. Cascades use the same execution pipeline as individual runs — same security model, no shortcuts, no shell involvement.

- **Cascade file management** — save, load, import, export, and delete named cascades. Cascade state persists across sessions.

- **Cascade variables** — define named variables with a type hint (string, file, directory, integer, float) and an apply-to scope (all steps, specific slots, or none). Before each cascade run, a dialog prompts for values and injects them into form fields across steps.

- **Three example cascades** — nmap reconnaissance chain (ping sweep → full port scan → stealth recon), curl API chain (health check → GET → POST), aircrack-ng capture chain (interface scan → capture → key analysis).

- **LLM-powered preset generation** — new `--preset-prompt` CLI flag prints a prompt template to stdout. Paste it alongside a tool schema into an LLM, describe the preset you want, drop the returned JSON into `presets/<tool>/`.

- **Password masking in command preview, output panel, and history** — password field values are displayed as `********` everywhere plaintext would otherwise appear.

- **Preset overwrite confirmation** — saving a preset whose filename already exists now prompts and pre-fills the description from the existing preset.

- **Preset picker filter bar** — live text filter with Enter / Shift+Enter to cycle through matches.

- **Tool picker keyboard navigation** — Enter in the filter selects the next matching tool, Shift+Enter goes to the previous.

- **Field search matches descriptions** — Ctrl+F now searches argument descriptions in addition to names and flags.

- **Output buffer cap (50 MB)** — when buffered output exceeds the cap, oldest entries are dropped and a truncation notice is added.

- **Extra flags unmatched-quote detection** — the Extra Flags box now highlights and blocks running if quotes are unbalanced, instead of silently falling back to naive whitespace splitting.

- **Docker tool schemas** — bundled schemas for `docker`, `docker-buildx`, `docker-compose`.

- **Cascade files dropped as tools show a clear rejection** — dragging a cascade JSON onto the app used to fail silently. Now shows a dialog pointing to the correct menu.

### Changed

- **License changed from MIT to PolyForm Noncommercial 1.0.0** — free for personal and noncommercial use; commercial use requires a separate license.

- **Tooltips escape user-supplied text** — flag names, descriptions, validation patterns, and other schema content are now HTML-escaped before being injected into tooltips, preventing rendering glitches from `<`, `>`, `&`.

- **Light-theme tooltips stay readable on mixed-theme Linux desktops** — tooltip colors are now set explicitly on theme switch.

- **Command preview colors negative numbers correctly** — tokens like `-1` or `-3.5` are now rendered as values, not flags.

- **PowerShell and CMD copy formats hardened** — both formatters now use a safe-character whitelist and strip literal newlines before quoting, using the shell-specific escape conventions (PowerShell `''`, CMD `""`).

- **Label decorations update on theme change** — dangerous (red warning prefix) and deprecated (strikethrough + amber suffix) labels now rebuild correctly when switching themes.

- **Arrow icon temp directory cleaned up on exit** — the temp folder Scaffold creates for UI icons is now deleted when the process exits.

- **Parent-field changes refresh the command preview for dependent children** — previously the preview lagged behind.

### Fixed

- **Duplicate flags within a scope are now caught at validation time** — two arguments sharing the same flag or short flag used to pass validation silently.

- **Non-string flag values no longer crash the validator** — a flag defined as an integer (from hand-edited or generated JSON) used to crash. Now reports a clear error.

- **Flag whitespace no longer bypasses duplicate detection** — leading or trailing whitespace in flag values used to let duplicates slip through. Now caught; whitespace is stripped during normalization.

- **Subcommand names with control characters or shell metacharacters now fail validation** — newlines, tabs, and shell characters are rejected.

- **Validator no longer crashes on non-standard input shapes** — `schema_hash` and the validator both now handle non-string flags, non-dict subcommands, and non-dict top-level input gracefully.

- **Non-numeric preset values no longer crash integer and float fields** — invalid values are silently skipped, leaving the field at its default.

- **Extra flags tooltip no longer truncates on some platforms** — wrapped in paragraph tags for consistent rendering.

- **Clear Output now also flushes the pending buffer** — buffered text used to reappear on the next flush cycle after clearing.

- **Stdout/stderr handlers no longer crash during cascade transitions** — both now guard against the process being None, which can happen briefly between cascade steps.

- **Recovery dialog suppressed during cascade chain loading** — used to interrupt cascade runs with an unwanted prompt.

- **Spinbox button alignment fixed in dark mode** — up/down buttons used to render slightly offset.

- **Warning bar rounded corners restored** — both themes.

#### Full suite results

- **All 6 test suites pass: 1,799/1,799 assertions, 0 failures**
  - Functional: 1,437/1,437
  - Security: 158/158
  - Smoke: 68/68
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23

## [v2.7.8] — 2026-04-04

Collapsible folder groups in the tool picker and run button sizing fix.

### Added

- **Collapsible folder groups** — tools in subfolders now appear at the top of the tool picker in collapsible groups. Click the folder header to expand or collapse its tools. Arrow indicators show the current state.

### Changed

- **Folder sort order** — subfolder groups are now pinned above root-level tools in the tool picker, sorted alphabetically.
- **Ansible schemas moved to subfolder** — the four ansible tool schemas now live in `tools/ansible/` to demonstrate the subfolder grouping feature.

### Fixed

- **Run button overlapping Clear Output** — the minimum width calculation now measures with a bold font to match the stylesheet, preventing the button text from clipping into adjacent buttons.

## [v2.7.7] — 2026-04-04

Dark mode clipping fixes and output toolbar spacing improvements.

### Fixed

- **Checkable group box title clipped in dark mode** — the checkbox and label text were rendered 1px above the widget boundary, cutting off the top row of pixels. Added top padding to the title sub-control to push it fully inside the visible area.
- **Run button text clipped when showing "Stopping..."** — the minimum width calculation now accounts for button padding, so the full label is always visible.
- **Timeout controls crowding into action buttons** — replaced the collapsible stretch with an expanding spacer that enforces a minimum 16px gap between the output buttons and the timeout controls.
- **Save Output button label** — removed the trailing ellipsis for consistency with the other action buttons.
- **Dark mode spinbox height** — reduced internal padding so spinboxes more closely match their native light mode height.

## [v2.7.6] — 2026-04-04

Copy format dropdown, output search bar auto-visibility, and tooltip spacing polish.

### Added

- **Copy format dropdown** — the Copy button now has a dropdown menu letting you choose between Bash, PowerShell, CMD, and Generic output formats. Your selection is remembered across sessions. The button label updates to show the active format (e.g., "Copy (Bash)").
- **Output search bar auto-visibility** — the search bar above the output panel now appears automatically when output is present and hides when the output is cleared, so it's always available when you need it.

### Changed

- **Tooltip spacing improved** — added a blank line between the flag/type header (e.g., `--branch -b (string)`) and the description text in field tooltips, making the two sections visually distinct.
- **Output action buttons use smaller font** — the Clear, Copy Output, and Save Output buttons now use 8pt text to reduce visual weight in the output toolbar.
- **Minimum window width increased** — raised from 500px to 530px to prevent the copy button dropdown from clipping.

### Tests

- Updated Section 27m to match new search bar visibility behavior (bar stays visible when output has content).
- Added Section 59 (23 assertions) covering copy format dropdown mechanics, QSettings persistence, button label updates, menu structure, and output search bar visibility lifecycle.
- Updated Section 5a minimum width assertion to match new 530px minimum.

#### Full suite results

- **All 6 test suites pass: 1,194/1,194 assertions, 0 failures**
  - Functional: 864/864
  - Security: 131/131
  - Smoke: 63/63
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23

## [v2.7.5] — 2026-04-04

Dark mode visual consistency, display group UX improvements, subcommand preview fix, and password serialization security hardening.

### Security

- **Password fields excluded from all serialization** — `serialize_values()` now skips fields with `type: "password"`, preventing secrets from being written to presets, crash recovery files, or command history. *(Suggested by Dan — thank you!)*
- **Recovery file permissions hardened** — `_autosave_form()` now calls `os.chmod(path, 0o600)` after writing recovery files, restricting access to the file owner on Linux/macOS. Wrapped in try/except for Windows compatibility.

### Fixed

- **Subcommand tokens swallowed in command preview** — `_colored_preview_html()` greedy flag-value lookahead was consuming subcommand tokens (e.g., `deploy` after `--flag`), making them invisible in the preview. Added a guard to skip pending subcommand parts.
- **Dark mode visual inconsistency** — added `border-radius: 3px` to all 9 QSS selectors that set `border` (QMenu, QCheckBox::indicator, QComboBox, QComboBox dropdown, QSpinBox/QDoubleSpinBox, QLineEdit, QListWidget, QPushButton, QPlainTextEdit, QToolTip). Added padding compensation to prevent height shrinkage: `3px` for QLineEdit, `3px 4px` for QComboBox, `1px 2px` for spinboxes, `4px 0` for QCheckBox, `1px` for QPlainTextEdit.
- **QGroupBox title position in dark mode** — added `subcontrol-origin: margin`, `padding: 0 4px`, `left: 8px` to `QGroupBox::title`, and `border-radius: 4px`, `margin-top: 14px`, `padding-top: 4px` to `QGroupBox` so titles render identically in both themes.
- **Display group collapse indicator missing** — group box titles now show ▾ (expanded) and ▸ (collapsed) indicators that update on toggle.
- **Display group clicks not reaching nested widgets** — replaced `mousePressEvent` lambda override with `installEventFilter()` pattern, which correctly dispatches events to child widgets inside collapsed/expanded groups.

### Changed

- **Gobuster schema restructured** — moved 12 global flags into all 7 subcommands to match gobuster v3.8.2's urfave/cli requirement that flags follow the subcommand token.

### Tests

- Updated Section 23 display group lookups to use `property("_dg_name")` instead of `title()` (title now includes ▾/▸ prefix).
- Updated Section 38f: password fields now verified as *excluded* from serialization (was *included*). Added 38f2 (mixed form serialization) and 38f3 (apply_values with missing password key).
- Added Section 46y (recovery file excludes password fields) and 46z (recovery file permissions on Linux).

#### Full suite results

- **All 6 test suites pass: 1,171/1,171 assertions, 0 failures**
  - Functional: 841/841
  - Security: 131/131
  - Smoke: 63/63
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23

## [v2.7.4] — 2026-04-03

Security audit, dedicated security test suite, deep audit bugfixes, and button clipping fix. The full updated codebase — after code audits by MiniMax M2.7, GitHub Copilot, and ChatGPT — was distilled back into Claude 4.6 Opus Extended Thinking, which found all of the bugs fixed in this release.

### Added

- **Security audit test suite (`test_security.py`)** — 131 focused security assertions across 8 sections: static analysis for forbidden patterns, shell metacharacter passthrough verification for all widget types, binary validation hardening, preset injection resistance, extra flags injection resistance, preset name path traversal, recovery file safety, and QProcess contract verification.

### Fixed

- **Recovery file crash on non-dict JSON** — `_check_for_recovery()` now validates that parsed recovery data is a dict before calling `.get()`, preventing an `AttributeError` when the file contains valid JSON that isn't a dict (e.g., a list).
- **Button text clipping during process execution** — the Run button now has a font-metrics-based minimum width calculated from its widest state ("Stopping..."), preventing layout reflow when cycling through Run/Stop/Stopping. All action bar and preview bar buttons use `QSizePolicy.Minimum` so they never shrink below their text content.
- **`_SHELL_METACHAR` recreated on every call** — moved from a local `set()` inside `validate_tool()` to a module-level `frozenset` constant.
- **Null bytes pass binary validation** — added `\x00` to `_SHELL_METACHAR` and a dedicated "contains null bytes" error message.
- **Recovery file collisions on shared systems** — added a per-user identifier to recovery filenames.
- **Underscore-prefixed flags collide with preset metadata** — `_validate_args()` now rejects flags starting with `_`.

### Security

The security model (no shell, no eval/exec, no network, typed constraints) was audited by a penetration tester and validated across multiple LLM-assisted review stages — including multi-model code review, targeted fuzzing of the command builder, and the new automated security test suite.

#### Full suite results

- **All 6 test suites pass: 1,167/1,167 assertions, 0 failures**
  - Functional: 837/837
  - Security: 131/131
  - Smoke: 63/63
  - Manual verification: 61/61
  - Examples: 52/52
  - Preset validation: 23/23

## [v2.7.3] — 2026-04-03

UI spacing polish and reset-confirmation fix. Security model audited using multi-LLM code review, manual code review, and assessment by a trained penetration tester.

### Changed

- **Command preview section grouped into a single layout** — the "Command Preview" label, preview bar, and status label are now wrapped in a dedicated `QVBoxLayout` with 5px internal spacing and zero margins, tightening all gaps in the preview area.
- **Preview label padding zeroed** — `preview_label` top/bottom padding overridden to 0px (scoped to preview only, not output label). Theme toggle re-applies the override to prevent resets.
- **Button spacing in preview bar** — added 12px vertical spacing between "Reset to Defaults" and "Copy Command" buttons to prevent accidental mis-clicks.

### Fixed

- **Reset confirmation used a permanent hidden widget** — replaced the dedicated `reset_status` QLabel (which consumed 16px of dead space even when empty) with a deferred `QTimer.singleShot(0)` call to `_show_status`, reusing the existing status label. The deferred write survives the `_update_preview` signal cascade triggered by `reset_to_defaults()`.

#### Full suite results

- **All 5 test suites pass: 1,036/1,036 assertions, 0 failures**
  - Functional: 837/837
  - Smoke: 63/63
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.7.2] — 2026-04-02

Bugfixes and UI improvements informed by a full deep code audit and hands-on user testing with a MiniMax M2.7 OpenClaw agent.

### Changed

- **"Reset to Defaults" moved to command preview area** — relocated from the Presets menu to a dedicated button stacked vertically above "Copy Command" in the preview bar. Disabled when no tool is loaded.


### Fixed

- **Drag-drop case sensitivity** — `dragEnterEvent` and `dropEvent` now use case-insensitive matching for `.json` extensions. Files named `tool.JSON` or `tool.Json` are no longer silently rejected.
- **Missing `_format` on bundled schemas** — added `"_format": "scaffold_schema"` as the first key in `git.json`, `nmap.json`, `ping.json`, and `curl.json`. These four schemas were missing the marker that identifies them as Scaffold schemas.
- **Reset status message not auto-clearing** — replaced `statusBar().showMessage()` with a dedicated `reset_status` label that auto-clears after 3 seconds via `QTimer.singleShot`.
- **Minimum window width increased to 500px** — raised from 420px to accommodate the new button layout.

### Added

- **aircrack-ng tool schema (testing)** — added `tools/aircrack-ng.json`. Mostly untested and may not cover every flag or edge case — contributions and bug reports welcome.


#### Full suite results

- **All 5 test suites pass: 1,036/1,036 assertions, 0 failures**
  - Functional: 837/837
  - Smoke: 63/63
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.7.1] — 2026-04-02

Hardening pass — four targeted fixes distilled from a full deep code audit with Copilot. Started with a comprehensive audit, iterated on the findings, and narrowed down to these surgical improvements.

### Fixed

- **BOM stripping in `load_tool`** — strip a leading UTF-8 BOM (`\ufeff`) before JSON parsing. Windows editors (especially Notepad) insert this character, which caused a cryptic `JSONDecodeError`.
- **QApplication guard in `_make_arrow_icons`** — return early with an empty string if no `QApplication` instance exists, preventing a crash when the function is called before the app is created (e.g., during test imports).
- **`normalize_tool` deep copy** — the function now returns a deep copy of the normalized data, so callers get an independent snapshot and won't see unexpected mutations through shared references. All internal and test call sites updated to use the return value; `ToolForm.__init__` now normalizes its own input defensively.
- **Flag stripping in `_check_duplicate_flags`** — trailing whitespace in hand-edited schema flag values (e.g., `"--port "` vs `"--port"`) no longer bypasses duplicate detection.

#### Full suite results

- **All 5 test suites pass: 1,034/1,034 assertions, 0 failures**
  - Functional: 836/836
  - Smoke: 62/62
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.7.0] — 2026-04-02

### Changed


- **Moved "Command History..." from Presets menu to View menu** — history is now under View > Command History (Ctrl+H), separated from preset management. The action is explicitly enabled/disabled based on whether a tool is loaded.
- **Renamed Cancel/Close buttons to "Back" in PresetPicker** — the dismiss button in the preset picker dialog now reads "Back" in all modes (load, edit, delete) for consistency with the tool picker.

### Added

- **Back button on tool picker** — the tool picker's button bar now has a Back button that returns to the previously loaded tool form. Disabled on first launch when no tool has been loaded; enabled when navigating to the picker from a loaded form.
- **Resizable table columns** — the tool picker, preset picker, and command history dialog columns can be manually resized by dragging column headers. The last column automatically fills remaining space with a 50px minimum.
- **Description tooltips** — hovering over tool descriptions in the picker or preset descriptions in the preset dialog shows the full text in a tooltip, helpful when descriptions are truncated by column width.

### Fixed

- **Recovery file reappears after declining and closing** — closing the app now stops all timers before clearing the recovery file, preventing the autosave timer from recreating the file during shutdown.
- **Phantom recovery prompts on tools with default values** — unchanged forms are no longer saved. The autosave system compares the current form state against a baseline captured at tool load, and only writes when actual changes exist.
- **Stale recovery files accumulating in temp directory** — expired or corrupted recovery files are automatically cleaned up on startup.
- **Up to 30 seconds of work lost on crash** — replaced the 30-second periodic autosave timer with a 2-second debounced single-shot timer. Every field change restarts a 2-second countdown; at most 2 seconds of work is lost on a crash.
- **Extra flags toggle ignored by command preview** — unchecking the Additional Flags group now removes extra flags from the command preview as expected.
- **Repeatable boolean audit** — fixed non-repeatable flags incorrectly marked `repeatable: true`: `tools/curl.json` `--verbose` (`-v`) and `tools/git.json` `--verbose` (`-v`). Both are single-toggle flags, not stackable like nmap's `-v -v -v`.
- **Test suite recovery file leaks** — all three test files that create `MainWindow` instances now include a cleanup helper that removes stale recovery files at startup and after final cleanup.

#### Full suite results

- **All 5 test suites pass: 1,026/1,026 assertions, 0 failures**
  - Functional: 832/832
  - Smoke: 58/58
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.6.9] — 2026-04-01

### Added

- **Copy As shell formats** — right-click the command preview area to copy the command formatted for Bash (`shlex.join`), PowerShell (single-quote quoting, `&` prefix for spaced binaries), or Windows CMD (double-quote quoting with special character handling). The existing Copy Command button and Ctrl+C shortcut remain unchanged.
- **Ansible tool schemas (testing)** — added 4 new schemas in `tools/Ansible/`: `ansible`, `ansible-playbook`, `ansible-vault`, and `ansible-galaxy`. These are mostly untested and may not cover every flag or edge case — contributions and bug reports welcome.

#### Full suite results

- **All 5 test suites pass: 960/960 assertions, 0 failures**
  - Functional: 766/766
  - Smoke: 58/58
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.6.8] — 2026-04-01

### Added

- **Portable mode** — if `portable.txt` or `scaffold.ini` exists next to `scaffold.py`, all settings are stored in a local `scaffold.ini` (INI format) instead of the system registry/plist. This lets users run Scaffold from a USB drive with fully isolated configuration. No UI changes; portable mode is detected automatically at startup from file existence.
- **Portable mode documentation** — `--help` output now mentions portable mode; README includes a new "Portable Mode" section with setup instructions.

### Fixed

- **Reduced minimum window width** — minimum width lowered from 640px to 420px, allowing narrower window arrangements on small monitors or side-by-side layouts.

#### Full suite results

- **All 5 test suites pass: 936/936 assertions, 0 failures**
  - Functional: 742/742
  - Smoke: 58/58
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.6.7] — 2026-04-01

### Added

- **Command history (Ctrl+H)** — after each command execution, Scaffold automatically records what was run. Press Ctrl+H (or Presets > Command History...) to browse recent commands for the current tool. Double-click or select "Restore" to reload the form state from any past run.
  - Stores up to 50 entries per tool in QSettings (display string, exit code, timestamp, form state)
  - Form state captured at run start, not finish — safe even if user changes the form while running
  - History dialog shows exit code (color-coded green/red), command, timestamp, and relative age
  - "Clear History" button with confirmation prompt
  - Per-tool isolation: each tool's history is independent
  - Graceful handling of corrupted QSettings data

### Fixed

- **Ambiguous Ctrl+H shortcut** — the history shortcut was registered as both a `QShortcut` and a menu action shortcut, causing Qt to log "Ambiguous shortcut overload" and ignore the keypress. Removed the duplicate `QShortcut`; the menu action handles it alone.

#### Full suite results

- **All 5 test suites pass: 924/924 assertions, 0 failures**
  - Functional: 730/730
  - Smoke: 58/58
  - Examples: 52/52
  - Manual verification: 61/61
  - Preset validation: 23/23

## [v2.6.6] — 2026-04-01

### Added

- **Form auto-save & crash recovery** — form state is automatically saved to a temporary recovery file every 30 seconds. If the app crashes or is force-closed, the next launch offers to restore the unsaved work via a recovery prompt. Recovery files expire after 24 hours and are cleared on normal close.
  - Auto-save timer starts when a tool is loaded, stops when returning to the picker
  - Recovery files stored in the system temp directory, keyed by tool name
  - Expired, mismatched, or corrupted recovery files are silently cleaned up
  - Empty/default forms are not saved (no unnecessary recovery prompts)
  - Immediate auto-save on first field change (no 30-second gap before first save)
- **Multi-word subcommand names** — subcommand names like `"role install"`, `"compose up"`, or `"compute instances create"` are now split into separate command tokens during assembly. Single-word subcommands are unaffected. Enables schemas for tools like `ansible-galaxy`, `docker compose`, `aws s3`, and `gcloud compute`.
  - Preview colorizer matches each subcommand word in sequence
  - Validation rejects empty names, leading/trailing whitespace, double spaces, and duplicates
  - New test schema: `tests/test_multiword_subcmd.json` (3 subcommands, global + scoped flags)
  - Updated `PROMPT.txt`, `schema.md`, and `README.md`

### Fixed

- **Repeat spinner clipping** — the "x" count spinner on repeatable boolean fields (e.g. nmap's Verbose `-v`) was too narrow, clipping the number text. Increased `REPEAT_SPIN_WIDTH` from 60px to 75px. Audit: the timeout spinbox (80px) and subcommand combo (600px max) are adequate; no other spinboxes are affected.
- **Required field status disappears after 3 seconds** — the red "Required: ..." message below the command preview was using `_show_status()`, which starts a 3-second auto-clear timer. Required field messages now set the label directly and stop the timer, persisting until the user fills in the missing fields. Transient messages (copy confirmations, etc.) still auto-clear normally.
- **Test suites hang on stale recovery files** — auto-save recovery files left behind by crashed sessions caused a blocking `QMessageBox.question()` dialog on the next test run. Added global `QMessageBox.question` auto-decline patches to all 3 test files.

#### Full suite results

- **All 5 test suites pass: 891/891 assertions, 0 failures**
  - Functional: 697/697
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 58/58
  - Preset Validation: 23/23
- All 10 tool schemas validate with zero errors

---

## [v2.6.5] — 2026-03-31

### Added

- **Format markers (`_format`)** — tool schemas and presets now include a `_format` metadata key (`"scaffold_schema"` and `"scaffold_preset"` respectively) to prevent cross-loading. Three-tier enforcement:
  - Correct `_format` → loads silently
  - Missing `_format` on tool schemas → warning dialog with "Load Anyway" / "Cancel" (presets with missing `_format` load silently for backwards compatibility)
  - Wrong `_format` → hard reject with clear error message explaining what the file appears to be
- **Example schema (`tools/example.json`)** — bundled reference schema demonstrating every Scaffold feature (all 10 widget types, subcommands, display groups, dependencies, mutual exclusivity, min/max, deprecated, dangerous, examples, validation, and more). Use it as a starting point when building schemas for your own tools.
- New presets automatically include `_format: scaffold_preset` as the first key. Existing presets without the marker continue to load fine.
- All 10 bundled tool schemas updated with `_format: scaffold_schema` as the first key.
- `PROMPT.txt` and `schema.md` updated to include `_format` in the schema specification and examples.

#### Full suite results

- **All 5 test suites pass: 825/825 assertions, 0 failures**
  - Functional: 631/631
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 58/58
  - Preset Validation: 23/23
- All 10 tool schemas validate with zero errors

---

## [v2.6.4] — 2026-03-31

### Added

- **Copy Output button** — copies the output panel's plain text to the clipboard. Placed in the action bar next to "Save Output...". Shows "No output to copy" if the panel is empty.
- **Tooltip word wrapping** — long tooltips on subcommand combo items and field descriptions now wrap instead of stretching across the screen. Tooltips use HTML rich text for automatic word wrapping.
- **Status message auto-clear** — transient status messages (copy confirmations, validation errors) now auto-clear after 3 seconds via a single-shot QTimer. Process state messages (Running, Exit) are unaffected.
- **About dialog GitHub link** — the Help > About Scaffold dialog now includes a clickable link to the GitHub repository.

#### Full suite results

- **All 5 test suites pass: 807/807 assertions, 0 failures**
  - Functional: 614/614
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23
- All 9 tool schemas validate with zero errors

---

## [v2.6.3] — 2026-03-31

### Fixed

- **Field search highlight jarring in dark mode** — highlight color now uses theme-aware selection color instead of hard-coded bright yellow.
- **Header separator stale color after theme toggle** — separator now updates when the user toggles themes.
- **Elevation note hard-coded gray** — now uses theme-aware dim text color.
- **Search no-match label hard-coded red** — now uses Catppuccin error color in dark mode.
- **Subcommand not colored in command preview** — subcommand tokens now render with a distinct bold color instead of generic value color.
- **ANSI escape codes in output panel** — CLI tools emitting ANSI escape codes (e.g. color sequences) no longer appear as raw text; they are stripped before display.
- **Documentation out of sync** — `password` type and six argument fields were missing from `schema.md`. All now documented.
- **Misleading comment** — reworded incorrect comment about `QTextDocument.find` case sensitivity.
- **Scroll position on subcommand switch** — switching subcommands now resets scroll to top.
- **Scroll area right margin** — removed unnecessary 8px right padding; `QScrollArea` handles scrollbar space internally.
- Dead code removal: removed unused methods and attributes that were defined but never called.

### Added

- **Help menu** — new "Help" menu in the menu bar with two items:
  - "About Scaffold" — app name, version, one-line description
  - "Keyboard Shortcuts" — listing of all 11 keyboard shortcuts
- **Subcommand combo box truncation** — long subcommand descriptions are truncated in the combo label with full text available in a tooltip.

#### Full suite results

- **All 5 test suites pass: 801/801 assertions, 0 failures**
  - Functional: 608/608
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23
- All 9 tool schemas validate with zero errors

---

## [v2.6.2] — 2026-03-31

### Fixed

- **Process cleanup missing for FailedToStart** — when `QProcess` emits `errorOccurred(FailedToStart)`, Qt does not emit `finished`. This left timers ticking, stale state, and process reference dangling. Added the same cleanup that normal finish performs.
- **"Stopping..." disabled button styling** — the "Stopping..." stylesheets lacked `:disabled` selectors, causing the amber styling to look washed out on some platforms. Added explicit disabled selectors for both themes.

#### Full suite results

- **All 5 test suites pass: 793/793 assertions, 0 failures**
  - Functional: 600/600
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23
- All 9 tool schemas validate with zero errors

---

## [v2.6.1] — 2026-03-31

### Fixed

- **Process kill hardening** — `QProcess.kill()` sends SIGKILL, which pkexec cannot forward to grandchild processes. Elevated commands would orphan the actual tool process when stopped. Now uses SIGTERM first (which pkexec can forward), with SIGKILL as a 2-second fallback.
- **Centralized kill logic** — all 5 process kill sites now call a single `_stop_process()` method instead of calling kill directly.
- **"QProcess: Destroyed while process is still running" warning** — process reference is now cleared on finish, preventing the warning on window close.
- **"Stopping..." button state** — after clicking Stop, the button shows "Stopping..." in amber/italic and is disabled until the process exits, preventing confusion if SIGTERM takes a moment.
- **"Stopping..." repaint on Linux** — forces Qt to repaint the button immediately before stopping the process, so the intermediate state is always visible.
- **Error handler button re-enable** — prevents the button from staying disabled if a QProcess error fires while in "Stopping..." state.

### Added

- SIGTERM-first process stop with 2-second escalation to SIGKILL
- Process group kill fallback for non-elevated non-Windows processes

#### Full suite results

- **All 5 test suites pass: 781/781 assertions, 0 failures**
  - Functional: 588/588
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23
- All 9 tool schemas validate with zero errors

---

## [v2.6.0] — 2026-03-31

### Added

#### Integer Min/Max Constraints

Optional `min` and `max` fields on integer and float argument types constrain the spinbox range in the GUI.

- **Schema fields:** `"min": null` and `"max": null`. Accept a number or null. Only valid on `integer` and `float` types — other types produce a validation error.
- **Backwards compatible:** Existing schemas without min/max normalize to null. No schema changes required.

#### Deprecated & Dangerous Flag Indicators

Two new optional argument fields provide visual indicators for flagged arguments without disabling them.

- **`deprecated`** (string or null): When set, the field label gets strikethrough and an amber "(deprecated)" suffix. The deprecation message is prepended to the tooltip. The field remains fully functional.
- **`dangerous`** (boolean, default false): When true, the field label gets a red warning symbol prefix. A caution message is prepended to the tooltip. Purely visual — no behavioral change.
- **Theme-aware colors** for both indicators.
- A field can be both deprecated and dangerous.

#### Password Input Type

New `password` widget type for flags that accept sensitive values like passwords, API keys, and tokens.

- Renders as a masked `QLineEdit` with a "Show" toggle checkbox.
- Works identically to string fields for command assembly, preset serialization, and preset loading.
- Supports `validation` regex (same as string). Using `examples` on a password type produces a validation warning.

#### Full suite results

- **All 5 test suites pass: 766/766 assertions, 0 failures**
  - Functional: 573/573
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23
- All 9 tool schemas validate with zero errors

---

## [v2.5.10] — 2026-03-31

### Added

- **Preset Picker dialog** — replaced the plain dropdown for Load/Delete Preset with a full table-based picker. 4 columns: favorite star, preset name, description, last modified date. Three modes: load, delete, edit.
- **Preset favorites** — click the star column to mark presets as favorites. Favorites sort to the top. Stored in QSettings with automatic cleanup of stale favorites.
- **Edit Description** — update a preset's description after saving via the preset picker.
- **Edit Preset mode** — "Edit Preset..." menu item opens the picker with "Edit Description..." and "Delete" buttons. Delete includes confirmation dialog with git restore tip.

### Changed

- **Presets menu restructured** — "Delete Preset..." replaced with "Edit Preset..." which opens the picker in edit mode

#### Full suite results

- **All 5 test suites pass: 710/710 assertions, 0 failures**
  - Functional: 517/517
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
  - Preset Validation: 23/23

---

## [v2.5.8] — 2026-03-31

### Added

- **Preset descriptions** — optional description field when saving presets, displayed as "name -- description" in the load/delete picker. Stored as `_description` in the preset JSON. Fully backwards-compatible with existing presets.

---

## [v2.5.7] — 2026-03-30

### Changed

- **Delete Tool** rebuilt as a button on the tool picker instead of a File menu action — simpler and more discoverable
- Delete button disabled by default, enables only when a valid tool is selected

### Fixed

- **Delete Tool** file menu action was permanently greyed out — replaced with picker button that tracks selection state

---

## [v2.5.6] — 2026-03-30

### Fixed

- **Preset delete dialog** now includes a git restore tip matching the tool schema delete dialog
- **Delete Tool tests** now exercise the real code path with mocked dialogs instead of bypassing with direct calls

---

## [v2.5.5] — 2026-03-30

### Added

#### Process Timeout

Auto-kill processes that run too long. Useful for fire-and-forget execution of tools like `nmap` or `hashcat` where the user may walk away.

- **Timeout spinbox** in the action bar next to the Run/Stop button. Label: "Timeout (s):", range 0-99999, default 0 (no timeout).
- If the timer fires before the process finishes, the process is killed with a warning message.
- Timer is stopped when the process finishes normally, when the user clicks Stop, or when a new tool is loaded.
- **Per-tool persistence** — timeout value saved to QSettings and restored when the tool is loaded. Each tool remembers its own timeout independently.
- Status bar shows "Timed out (Ns)" instead of "Process stopped" for timeout kills.

#### Full suite results

- **All 4 test suites pass: 576/576 assertions, 0 failures**
  - Functional: 406/406
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.5.4] — 2026-03-30

### Added

#### Output Export (Save Output...)

Save command output to a text file for later review or sharing.

- **"Save Output..." button** added to the action bar, next to "Clear Output".
- Native save dialog with a suggested filename: `{tool_name}_output_{YYYYMMDD_HHMMSS}.txt`, defaulting to the user's home directory.
- Plain text export — strips all formatting. Includes the command echo line if present.
- UTF-8 encoding for saved files.
- Empty output guard — shows "No output to save" in the status bar if the output panel is empty.
- Status bar confirmation on success or error.

#### Full suite results

- **All 4 test suites pass: 564/564 assertions, 0 failures**
  - Functional: 394/394
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.5.3] — 2026-03-30

### Added

#### Output Search (Ctrl+Shift+F)

Search within the output panel to find text in command output. Separate from the form field search (Ctrl+F).

- **Search bar** appears above the output panel when Ctrl+Shift+F is pressed. Initially hidden.
- **Highlighting:** All matches highlighted in yellow, current match in orange. Highlights cleared when search is closed.
- **Navigation:** Enter jumps to next match, Shift+Enter to previous. Wrap-around at both ends.
- **Match count:** Label shows "X of Y" or "0 matches".
- **Case-insensitive** search.
- **Escape handling:** Escape closes the output search bar only when it has focus — does not steal Escape from the Stop button or the field search bar.

#### Full suite results

- **All 4 test suites pass: 553/553 assertions, 0 failures**
  - Functional: 383/383
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.5.2] — 2026-03-30

### Fixed

#### Output Panel Drag Handle Off-Screen Bug

The output panel resize handle allowed dragging the panel taller than the visible window area. The oversized height was saved to QSettings, so the problem persisted across restarts.

- Drag limit now dynamically capped to half the actual window height.
- Output height is clamped on window resize and on first display, fixing any oversized height restored from settings.

#### Full suite results

- **All 4 test suites pass: 528/528 assertions, 0 failures**
  - Functional: 358/358
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.5.1] — 2026-03-30

### Added

#### Syntax-Colored Command Preview

The command preview panel now displays syntax-colored output. Each token type is rendered in a distinct color for instant visual parsing of the assembled command.

- **Token types:** binary (bold blue), flag (amber/orange), value (default text), subcommand (bold blue), extra (dimmed gray, italic).
- **Light and dark mode** palettes with Catppuccin-inspired dark colors.
- **Preview widget** uses `QTextEdit` for rich text support — still a native PySide6 widget, no web engine.
- **Copy Command** still copies plain text — no HTML in clipboard.
- **Theme toggle** re-colors the preview immediately.
- Tokens with spaces or shell metacharacters are single-quoted for display. `--flag=value` is split with flag-colored flag part and value-colored value part.

#### Full suite results

- **All 4 test suites pass: 520/520 assertions, 0 failures**
  - Functional: 350/350
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
- All 9 tool schemas validate with zero errors

---

## [v2.5.0] — 2026-03-30

### Added

#### Collapsible Argument Groups -- `display_group` field

New optional argument field `display_group` (string or null) enables visual grouping of related arguments into collapsible sections. Arguments sharing the same `display_group` value are rendered inside a `QGroupBox` with that value as the title. Clicking the header toggles visibility.

- **Schema field:** `"display_group": null`. Accepts a string (group name) or null (ungrouped -- renders flat as before).
- **Normalization:** Existing schemas remain valid with no changes; missing keys fill to null.
- **Subcommand support:** Display groups work identically within subcommand argument sections.
- **No impact on existing behavior:** Command assembly, presets, mutual exclusivity, dependencies, and all widget types are unchanged.

#### Field Search / Jump -- Ctrl+F

Always-visible search bar below the tool header. Users can type to search or press Ctrl+F to focus it. Searches field names and flag names across all visible fields.

- Case-insensitive substring matching against both argument name and flag.
- First match is highlighted and scrolled into view. Enter/Shift+Enter cycle through matches.
- Collapsed display groups are automatically expanded when a match is inside them.
- Only searches visible scopes (global args + current subcommand).
- **Shortcuts:** Ctrl+F focuses, Escape clears and defocuses.

#### Full suite results

- **All 4 test suites pass: 494/494 assertions, 0 failures**
  - Functional: 324/324
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
- All 9 tool schemas validate with zero errors

---

## [v2.4.0] — 2026-03-30

### Added

- Added `display_group` field for collapsible argument groups -- see v2.5.0 for details.

#### Full suite results

- **All 4 test suites pass: 465/465 assertions, 0 failures**
  - Functional: 295/295
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57
- All 9 tool schemas validate with zero errors

---

## [v2.3.1] — 2026-03-30

### Added

#### Tool Picker Search/Filter
- Search bar at the top of the tool picker with placeholder "Filter tools..."
- Filters by tool name, description, and path columns (case-insensitive)
- Clear button restores all rows; auto-focuses on picker display

#### Keyboard Navigation
- Enter/Return key opens the selected tool from the picker
- Tab order: search bar -> table for natural keyboard flow
- Numpad Enter also supported

#### Enhanced Tooltips
- Tooltips now show flag, short_flag, type, separator mode, description, and validation regex
- Applied to both field labels and widgets
- Boolean fields hide separator; positional fields show placeholder name

#### Status Bar Improvements
- On tool load: shows field count and required count
- During execution: elapsed timer ticks every second
- On completion: elapsed time in exit message

#### Full suite results

- **All 4 test suites pass: 441/441 assertions, 0 failures**
  - Functional: 271/271
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.3.0] — 2026-03-29

### Added

#### Dependency Validation at Schema Load Time

`validate_tool()` now checks that every `depends_on` reference points to an existing flag within the same scope. Previously, a broken reference was silently ignored at load time and only failed at runtime.

#### Widget Creation Fallback

If an unexpected argument configuration causes an exception during widget construction, the form no longer crashes -- a plain `QLineEdit` fallback is returned with a tooltip explaining the failure. One bad field does not break the rest of the form.

#### Preset Schema Versioning

Presets now include a `_schema_hash` field -- an 8-character hash of the tool's argument flags at save time. When loading a preset against a changed schema, the status bar shows a non-blocking warning. Old presets without the hash load normally.

#### Runtime Dependency Audit (GUI)

After dependency wiring fails to find a parent widget, a warning is logged to stderr and the dependent field is left enabled (fail-open) instead of silently skipped.

### Improved

#### Flagship Schema Polish (nmap)

- All 111 argument descriptions rewritten in plain English
- Examples populated on key string fields (`--script`, `-p`, `TARGET`)
- Three ready-to-use presets: `quick_ping_sweep`, `full_port_scan`, `stealth_recon`

#### Full suite results

- **All 4 test suites pass: 343/343 assertions, 0 failures**
  - Functional: 173/173
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.2.0] — 2026-03-29

Public release of Scaffold. All changes from v2.1.0 and v2.1.1 are included.

### Changed
- README disclaimer updated: assertion count corrected from 157 to 291

### Tested
- **All 4 test suites pass: 291/291 assertions, 0 failures**
  - Functional: 121/121
  - Examples: 52/52
  - Manual Verification: 61/61
  - Smoke: 57/57

---

## [v2.1.1] — 2026-03-29

### External Code Review -- MiniMax M2.7

The full codebase was submitted to MiniMax M2.7 for an independent code review. Three bugs were fixed, three new test sections added, and PROMPT.txt was improved.

### Fixed

#### Spinbox value 0 silently dropped from command

Integer and float fields with no schema default used 0 as both the "empty/unset" sentinel and as a valid user value. This caused flags like `-m 0` (hashcat MD5 mode) or `--rate 0.0` to be silently omitted from the assembled command. The fix uses -1/-1.0 as the sentinel instead, so value 0 is now correctly included in the command.

#### No visual feedback on invalid Additional Flags input

The Additional Flags free-text field uses `shlex.split()` to tokenize user input. When the input contains unclosed quotes, the code silently fell back to naive splitting. Added a red border (theme-aware) on invalid input to warn the user. The fallback to naive splitting is preserved -- the border is a warning, not a blocker.

#### PROMPT.txt improvements

- Rule 8 completeness directive rewritten: now says "Include ALL documented flags" without the contradictory cap at 30-50.
- No-duplicates checklist rule clarified: duplicates are per-scope, not global. The same flag may appear in multiple subcommands.
- New coverage self-check section added: instructs the LLM to count arguments and include a `_coverage` field.

### Regenerated Schemas

Three tool schemas were regenerated with the updated full-coverage directives:

| Schema | Before | After | `_coverage` |
|--------|--------|-------|-------------|
| **nmap.json** | 34 args | 111 args | `full` |
| **curl.json** | 50 args | 271 args | `partial` (3 meta flags skipped) |
| **hashcat.json** | 46 args | 136 args | `full` |

All 9 schemas pass `scaffold.py --validate` with zero errors.

### Tested

Four test suites now cover the codebase. **Total: 291 assertions, 0 failures.**

- Functional: 121/121 (+16 new)
- Examples: 52/52 (unchanged)
- Manual Verification: 61/61 (new suite -- spinbox 0 edge cases, extra flags validation, dark mode colors, corrupted presets)
- Smoke: 57/57 (new suite -- full user workflow: launch, load, form, preview, run, presets, window behavior)

---

## [v2.1.0] — 2026-03-29

### Added
- **OpenClaw tool schema** (`tools/openclaw.json`) -- AI agent platform with 10+ subcommands
- **Gateway `--restart` flag** -- added missing boolean to the openclaw gateway subcommand
- **Linux install troubleshooting** -- README note covering `--break-system-packages` workaround and `libxcb-cursor0` dependency
- **Crypto donation addresses** -- BTC, LTC, ETH, BCH, and SOL

### Fixed
- **Dark mode scrollbars now match light mode exactly** -- uses `QStyleHints.setColorScheme(Qt.ColorScheme.Dark)` for native dark scrollbar rendering. Gracefully falls back on Qt < 6.8.

### Improved
- Side-by-side screenshots in README
- README updates -- line-count badge, assertion count, openclaw added to schemas table

### Tested
- Functional: 105/105 pass
- Examples: 52/52 pass
- Scrollbar visual parity confirmed on Windows 11 with PySide6 6.11.0

---

## [v2.0.1] — 2026-03-29

### Fixed
- **README assertion count** -- second mention still said 661; corrected to 156

---

## [v2.0.0] — 2026-03-29

### Added
- **`__version__` constant** (`2.0.0`) and `--version` / `-V` CLI flag
- **Shebang line** for direct execution on Unix (`./scaffold.py`)
- **Resizable output panel** -- drag handle between command controls and output panel (80px-800px range, persisted via QSettings)
- **Command options border** -- form area wrapped in a rounded frame for visual distinction
- **Separator line** between tool header and command options
- **UI polish** -- centered section labels, separator lines, green Run / red Stop button styling
- New example tool schemas: `nikto.json`, `gobuster.json`, `hashcat.json`, `ffmpegv2.json`
- Validation feedback tip in README

### Improved
- Tighter vertical spacing to reclaim screen space
- Higher contrast light theme borders
- `--help` output now includes version number
- **PROMPT.txt rewrite:**
  - Added complete example JSON demonstrating all key patterns
  - Added type hallucination guard table with wrong-to-correct name mappings
  - Expanded separator detection guidance, flag/short_flag convention, and completeness rules
  - Compressed to ~1,039 words while preserving all rules

### Fixed
- **QPushButton stylesheet parse error** -- missing whitespace between QSS rule blocks
- **`nmap.json` `-sC` mislabeled** -- `-sC` is a distinct flag, not the short form of `--script`. Split into separate entry.

### Tested
- Full pre-release audit (security, cross-platform, CLI entry points, error handling, UI polish)
- Functional: 104/104 pass
- Examples: 52/52 pass
- All 8 bundled tool schemas pass validation

---

## [v1.4.0] — 2026-03-28

### Added
- **`--help` / `-h` flag** with usage summary listing all available CLI modes
- Expanded module-level docstring with usage examples

### Improved
- Linted with `ruff check` -- all checks pass
- **Output buffering** -- output panel now buffers data and flushes every 100ms, preventing UI stalls on high-volume output
- **Output line cap** -- capped at 10,000 lines to bound memory usage
- **File size guard** -- tool schema loader rejects files over 1 MB
- **Error handling audit** -- added `PermissionError` handler in loader, try/except on preset save/delete, guards against file-at-directory-path
- **Cleanup** -- extracted magic numbers into named constants, organized imports, added docstrings and type hints to all key functions, simplified several methods

### Tested
- Functional: 101/101 pass
- Verified: no signal duplication on subcommand switch, no memory leaks on tool switch, `--prompt` output on Windows cp1252

---

## [v1.3.1] — 2026-03-28

### Improved
- Added right padding between form fields and scrollbar for cleaner layout

---

## [v1.3.0] — 2026-03-28

### Added
- **Elevated execution (sudo/admin) support** -- run commands with elevated privileges from within Scaffold
- New `elevated` schema field: `null`, `"never"`, `"optional"`, or `"always"`
- Platform-specific elevation: `gsudo` on Windows, `pkexec` on Linux/macOS
- Elevation checkbox in tool form with contextual notes
- "Running as administrator" status bar indicator when app is already elevated
- Elevation state included in preset save/load
- Graceful handling of missing elevation tools and cancelled password dialogs
- Custom arrow icons for dropdown and spinbox controls in dark mode
- Updated `PROMPT.txt` with `elevated` field and decision rule
- All example tool schemas updated with `elevated` field

### Security
- Full command injection audit passed -- QProcess uses list-based execution, no shell invocation

---

## [v1.2.3] — 2026-03-28

### Improved
- Added prerequisites section to Getting Started
- Added platform compatibility note

---

## [v1.2.2] — 2026-03-28

### Added
- **Dark mode** with system detection and manual toggle (View > Theme menu, Ctrl+D shortcut)
- Three theme options: Light, Dark, and System Default -- persisted across sessions
- Catppuccin Mocha-inspired dark color palette with full widget coverage
- Getting started instructions at the top of README

---

## [v1.2.1] — 2026-03-28

### Improved
- Command preview bar now uses a scrollable widget with a horizontal scrollbar for long commands

---

## [v1.2.0] — 2026-03-28

### Added
- **Examples field** for string arguments -- editable dropdown showing common values as suggestions while still allowing custom input
- Updated `PROMPT.txt` to include examples field (15 fields per argument)
- Updated all example tool schemas with expanded argument coverage and examples support

---

## [v1.1.0] — 2026-03-28

### Added
- **Binary availability check** in tool picker -- green checkmark for installed tools, red X for missing ones
- Tools sorted by availability (available first, unavailable second, invalid last)
- 4-column tool picker table (status, tool, description, path)

---

## [v1.0.1] — 2026-03-28

### Added
- ffmpeg example schema (later removed as incomplete)

---

## [v1.0] — 2026-03-28

### Added
- Initial release of Scaffold
- Single-file PySide6 application (`scaffold.py`)
- 9 widget types: boolean, string, text, integer, float, enum, multi_enum, file, directory
- Live command preview with copy-to-clipboard
- Process execution with colored stdout/stderr output
- Preset save/load/delete/reset with JSON persistence
- Subcommand support (e.g., git clone, git commit)
- Mutual exclusivity groups (radio-button behavior for conflicting flags)
- Dependency chains (fields activate when parent is set)
- Regex validation for string fields
- Repeatable boolean flags with count spinner
- Separator modes: space, equals, none
- Session persistence (window size/position, last opened tool)
- Drag-and-drop schema loading
- Keyboard shortcuts (Ctrl+Enter, Escape, Ctrl+S, Ctrl+L, etc.)
- CLI validation mode (`--validate`)
- LLM prompt for schema generation (`--prompt` / `PROMPT.txt`)
- Example schemas: nmap, ping, git, curl
- README with full documentation and schema reference
- MIT license
