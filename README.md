Scaffold — Your CLI Tools, but with Buttons

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52?logo=qt&logoColor=white)
![Single File](https://img.shields.io/badge/architecture-single%20file-blue)
![Lines of Code](https://img.shields.io/badge/lines-10%2C300%2B-informational)
![Tests](https://img.shields.io/badge/tests-3%2C798%20assertions-brightgreen)
![Test Suites](https://img.shields.io/badge/test%20suites-6-brightgreen)
![Bundled Tools](https://img.shields.io/badge/bundled%20tools-28%20schemas-orange)
![No Shell](https://img.shields.io/badge/shell%3DTrue-never-critical)
![Fully Offline](https://img.shields.io/badge/network-fully%20offline-success)
![License](https://img.shields.io/badge/license-AGPL%20v3-blue)

Stop memorizing flags. Start clicking them.

Scaffold turns command-line tools into native desktop GUIs. Most CLIs that take flags and positional arguments can be described in a JSON schema, Scaffold simply reads the schema and builds the visual form. Every option becomes visible, typed, and clickable instead of memorized. Adding a new tool is usually a two-step process: paste the included schema prompt plus the tool's docs into an LLM, then drop the returned JSON into the `tools/` folder. It works well for standard binaries, shell scripts, `.cmd` files, and Python scripts.

If a CLI can be described, it can be clicked. When a tool fits the flag/positional/subcommand model and the schema captures its arguments precisely, Scaffold produces a form that's genuinely better than the CLI it wraps — every flag visible, every value typed, every constraint enforced at entry, and noticeably faster to use than typing the command by hand. A precise schema produces a precise form. For tools that fit the model cleanly, the GUI tends to become the preferred interface, not a fallback. Tools whose behavior depends on interactive state, environment detection, or stdin parsing are where the model strains — see [Limitations](#limitations).

No shell invocation, by design. Every command runs through PySide6's QProcess with arguments passed as a list — no shell parsing, no string interpolation, no command string assembly. Shell metacharacters in schemas or user input are treated as literal strings because nothing ever interprets them as shell syntax. This isn't sanitization; it's the absence of an attack surface. A dedicated security test suite (243 assertions) covers the contract and you can run it yourself via `python test_security.py`. Output streams live into the app, the command history window records past runs, and any output can be copied or saved to disk.

Scaffold works with many tools, not just well-known programs like nmap or ffmpeg. If your team has a custom deployment CLI, a database migration tool, or a build script with 30 flags nobody can remember, write a schema and your team gets a GUI. Massive tools with hundreds of flags (like curl's 271-argument schema) work too.

Tools don't have to be on your system PATH either. Add one or more custom directories via File > Custom PATH Directories and Scaffold will search them for binaries — useful for unpacked release archives, portable script collections, or per-project tool folders. Custom directories are used for both the tool picker's installed/not-installed detection and for process execution, so a binary resolved from a custom directory runs identically to one on the system PATH. Settings persist across sessions, and in portable mode they're stored in scaffold.ini alongside the app.

Presets save the work of reconstructing complex commands. Save your perfectly-tuned nmap recon scan, your go-to ffmpeg encoding pipeline, or your favorite hashcat attack config as a named preset with a description. Share preset files with your team. Don't want to build them by hand? Use the built-in LLM preset generation — describe what you want in plain English and drop the result into your presets folder.

Need to chain multiple tools together? The cascade system lines up sequential tool runs with per-step delays, loop mode, and runtime variables — all through the same QProcess pipeline. Cascades can be saved and shared. You can also generate them from an LLM (experimental) using a Claude Project preloaded with your schemas and presets.

Under the hood, Scaffold generates interactive forms from simple JSON schema files — dropdowns, checkboxes, file pickers, and a live syntax-colored command preview. Hand a tool's man page to an LLM with the bundled SCHEMA_PROMPT.txt and get a working schema back. Collapsible sections, field search, and display groups keep large forms manageable.

Single Python file, one dependency (PySide6), no telemetry, no network calls, fully offline.

<p>
  <img src="nmap%20example.png" alt="Scaffold running an nmap schema" width="48%">
  <img src="hashcat%20example.png" alt="Scaffold running a hashcat schema" width="48%">
</p>

> **Tip:** If a specific flag or argument isn't behaving the way you expect in the form, you can type it directly into the Additional Flags box at the bottom of any tool form. It passes your input as a string through the same QProcess.

---

## Installation

### Quick install

```bash
pip install git+https://github.com/kevlattice/scaffold.git@v2.13.1
```

This installs `scaffold` as a shell command. Launch the GUI:

```bash
scaffold
```

### Recommended for non-developers

[pipx](https://pypa.github.io/pipx/) installs Python apps in isolated
environments without conflicting with system Python:

```bash
pipx install git+https://github.com/kevlattice/scaffold.git@v2.13.1
```

### From source (for development)

```bash
git clone https://github.com/kevlattice/scaffold.git
cd scaffold
python scaffold.py
```

### Where files are stored

After pip-installing, your data lives in the platform's standard
application data directory:

| Platform | Location |
|----------|----------|
| Linux    | `~/.local/share/Scaffold/` |
| macOS    | `~/Library/Application Support/Scaffold/` |
| Windows  | `%APPDATA%\Scaffold\` |

Inside that directory you'll find `presets/`, `cascades/`, and `tools/`
(for adding your own schemas). Drop a `*.json` schema into the `tools/`
subdirectory and it appears in the picker the next time you launch.

### Portable mode

Place a `portable.txt` next to `scaffold.py` (source-clone only) to
keep all settings and user data in the script directory. Useful for
USB sticks or fully-isolated installs.

---

## Getting Started

### Prerequisites

- **Windows, macOS, or Linux** — tested primarily on Windows. PySide6 is cross-platform, so macOS and Linux should work but have had less testing. If you hit platform-specific issues, please open an issue.
- **Python 3.10+** — make sure Python is installed and up to date (`python --version`)
- **The CLI tool you want to use** — Scaffold builds a GUI for tools already installed on your system. For example, if you want to use the nmap schema, you need nmap installed and available in your PATH.
- **A JSON schema for your tool** — Scaffold comes with bundled schemas for 27 tools in the `tools/` folder (two of which — `pandoc` and `yt-dlp` — are experimental), plus example cascade files in `cascades/`. To add your own, see [Creating Tool Schemas](#creating-tool-schemas) below.

### Install and Run

```bash
# Clone the repo
git clone https://github.com/kevlattice/scaffold.git
cd scaffold

# Install the only dependency
pip install PySide6

# Launch Scaffold
python scaffold.py
```

The tool picker opens showing all `.json` schemas in the `tools/` folder (including subfolders). Tools in subfolders appear at the top in collapsible folder groups. A green checkmark means the tool is installed and ready to use; a red X means it's not found in your PATH. Double-click any tool to open its form, fill in the fields, and hit **Run**.

> **Tip:** Toggle dark mode with **Ctrl+D** or from the **View > Theme** menu.

> **Linux note:** On some distros (Ubuntu 24.04+), `pip install PySide6` may fail because the system Python is externally managed. Use a virtual environment: `python3 -m venv .venv && source .venv/bin/activate && pip install PySide6`. You may also need `sudo apt install libxcb-cursor0` for the Qt platform plugin.

To launch directly into a specific tool: `python scaffold.py tools/nmap.json`

---

## Features

- **Works with most CLI tools** — JSON schema in, native GUI out
- **Presets** — save, name, reload, and share form configurations
- **No shell invocation** — QProcess with list args, arguments passed directly to the target binary
- **Typed input constraints** — widgets validate values at entry
- **LLM-powered schema generation** — paste SCHEMA_PROMPT.txt + docs, get a schema
- **Syntax-colored command preview** — live preview with color-coded tokens
- **Process execution** — run, stop, search output, copy or save results
- **10 widget types** — checkbox, text, spinbox, dropdown, file picker, password with show/hide, and more
- **Subcommand support** — multi-level commands like `git clone` or `ansible-galaxy role install`
- **Command history** — browse and restore past runs per tool (Ctrl+H), with filter bar
- **Cascade history** — record, browse, and restore past cascade runs (Ctrl+Shift+H), with export
- **Password masking** — values replaced with `********` in command preview, output, history, and clipboard
- **Auto-save and crash recovery** — form state saved automatically, restored on next launch
- **Collapsible display groups** — organize large forms into sections
- **Field search** — find any field by name, flag, or description (Ctrl+F)
- **Output search** — search within command output (Ctrl+Shift+F) with match highlighting and navigation
- **Process timeout** — optional auto-kill after N seconds, per tool
- **Mutual exclusivity and dependencies** — radio groups and conditional fields
- **Validation** — regex patterns and required field checking
- **Drag and drop** — drop a `.json` schema to load it
- **Portable mode** — run from USB with local settings via `portable.txt`
- **Custom PATH directories** — add directories containing scripts or custom tools so Scaffold can find and execute them without modifying your system PATH
- **Elevated execution** — optional `pkexec` (Linux) or `gsudo` (Windows) elevation per tool
- **Cascade chaining** — chain multiple tool runs (up to 20 slots) with per-step delays, loop mode, stop-on-error, runtime variables, cascade captures, and saveable cascade files
- **LLM-powered preset generation** — generate presets from natural language with `--preset-prompt` and `PRESET_PROMPT.txt`
- **Copy as shell formats** — Bash, PowerShell, or CMD via right-click
- **Single Python file** — one file, one dependency (PySide6), fully offline
- **LLM-powered cascade generation (experimental)** — generate cascade files from natural language via a preconfigured Claude Project (or equivalent). See `CASCADE_LLM_GENERATION_GUIDE.md`





See [Detailed Features](#detailed-features) below for full descriptions.

---

## How It Works

Each CLI tool is described by a JSON schema file in the `tools/` folder. Here's a minimal example:

```json
{
  "_format": "scaffold_schema",
  "tool": "mytool",
  "binary": "mytool",
  "description": "What this tool does.",
  "subcommands": null,
  "arguments": [
    {
      "name": "Verbose",
      "flag": "-v",
      "type": "boolean",
      "description": "Enable verbose output"
    },
    {
      "name": "Target",
      "flag": "TARGET",
      "type": "string",
      "positional": true,
      "required": true,
      "description": "The target to operate on"
    }
  ]
}
```

Each argument declares its type (`boolean`, `string`, `integer`, `enum`, `file`, etc.), and Scaffold picks the right widget. Fields you don't specify fall back to sensible defaults. The full schema specification is in [schema.md](schema.md), and `tools/example.json` demonstrates every feature in one file.

### Creating Tool Schemas

**The easy way: use an LLM.** The included `SCHEMA_PROMPT.txt` contains a detailed prompt that teaches any LLM to generate schemas:

1. Copy the contents of `SCHEMA_PROMPT.txt`
2. Paste it into your LLM of choice
3. Say: *"Generate a Scaffold schema for [tool name]"* and paste the tool's man page or official documentation. `--help` output alone is usually not enough — it provides flag names but lacks the detail needed for correct types, descriptions, and dependencies. The richer the input, the better the schema.
4. Save the JSON response as `tools/toolname.json`
5. Restart Scaffold (or use File > Reload)

Always validate the result:

```bash
python scaffold.py --validate tools/toolname.json
```

If validation reports errors, paste them back into the LLM and ask it to fix them — most models correct their own mistakes in one round.

> **Note:** Complex tools with many flags work best with frontier-level LLMs (Claude Opus, GPT-4, Gemini Pro). Smaller models may truncate output or produce invalid JSON.

**Generate it automatically (Claude Code):** instead of hand-writing a `tools/*.json`, the **scaffold-gui** Claude Code skill generates schemas — and presets — for any CLI. Point it at your tool's `--help`/man page, or run it inside your CLI's repo and it reads the argument-parser source + docs to recover the relationships `--help` can't (mutually-exclusive groups, dependencies, enum choices, real hover descriptions), then validates the output before handing it back. → https://github.com/kevwillow/scaffold-claude-skill

**The manual way:** Copy `tools/example.json`, rename it, and modify it for your tool. The example schema covers all 10 widget types, subcommands, display groups, dependencies, mutual exclusivity, min/max constraints, deprecated and dangerous flags, editable suggestions, and validation patterns.

### Using tools that aren't on your PATH

The binary field in a schema expects either a bare name that resolves through your PATH (nmap, docker, git) or an absolute path (/opt/tools/installer on Unix, C:\Tools\installer.exe on Windows). Shell-relative paths like ./installer do not work — Scaffold doesn't invoke a shell, so there's nothing to evaluate the ./ prefix.
This most often trips up downloaded release binaries and script-based tools. If you extracted rayhunter's release zip and the schema says "binary": "installer", running it fails with "command not found" because installer isn't on your PATH. Three fixes, in order of preference:

Add the containing folder via Custom PATH Directories (File > Custom PATH Directories...). Cross-platform, doesn't modify your system, persists across sessions, and works identically for binaries and scripts. See the Custom PATH Directories section below for details. Best choice when you only need the binary available inside Scaffold.
Put the binary on your system PATH. On Unix, symlink it into /usr/local/bin (e.g. ln -s ~/rayhunter/installer /usr/local/bin/installer). On Windows, add the containing folder to your PATH environment variable.
Edit the schema to use an absolute path. Change "binary": "installer" to "binary": "/home/you/rayhunter/installer" (or on Windows, "C:\\Tools\\rayhunter\\installer.exe" — note the doubled backslashes inside JSON strings).

Options 1 and 2 keep the schema portable across machines; the absolute-path approach is quicker when you only need it locally.

---

## Security

### No shell intermediary

Scaffold never uses `shell=True`. All command execution goes through `QProcess` with arguments passed as a Python list. There is no shell parsing, no string interpolation, and no command string assembly. Shell metacharacters in schemas or user input — pipes, semicolons, backticks, `$(...)`, glob patterns — are treated as literal strings because no shell ever interprets them. This eliminates shell injection by design, not by sanitization. The command preview shows exactly what will execute, token by token.

The `binary` field in every schema is validated at load time: shell metacharacters are rejected, relative paths with separators are rejected, and only bare executable names or absolute paths are accepted.

### Typed input constraints

Every argument passes through a typed widget that constrains values at the point of entry. A port number comes from a `QSpinBox` with defined bounds, not free text. Enum values come from a `QComboBox` with a fixed choice list. Boolean flags are checkboxes. File and directory arguments use native picker dialogs. The attack surface for malformed input is minimal because the GUI prevents it structurally.

The **Additional Flags** field is the only free-text path to the command line. It uses `shlex.split()` for tokenization with visual feedback on malformed input (unclosed quotes, etc.), and the resulting tokens are passed as discrete list items to `QProcess` — never concatenated into a shell string.

### No dynamic code execution

The codebase contains no `eval()`, no `exec()`, and no dynamic imports. Schema files are parsed as JSON data and treated as static declarations. Loading a schema reads structured data; it never executes code.

### No runtime network access

Scaffold makes zero network calls. No telemetry, no update checks, no analytics, no external API dependencies. The application runs entirely offline.

### Additional hardening

- **Atomic writes** — preset, autosave, and cascade files are written to a temp file then atomically renamed, preventing corruption on crash
- **Password masking** — password-type values are masked in the command preview, output panel, history entries, and clipboard (with a one-time confirmation prompt before exposing to clipboard)
- **Capture injection guard** — cascade captures that resolve to flag-like strings are caught and halt the chain under stop-on-error, preventing argv confusion

### Verify it yourself

Scaffold ships with a dedicated security test suite (`test_security.py`) that any user can run: static analysis for `shell=True`/`eval`/`exec`/`os.system`/`subprocess.*` patterns, shell metacharacter passthrough verification across every widget type, binary validation hardening, preset and extra-flags injection resistance, path traversal checks, recovery file safety, and direct QProcess contract verification.

```bash
python test_security.py
```

The security model has been reviewed across multiple LLM-assisted audit cycles.

---

## Detailed Features

### Works with most CLI tools

Most tools that accept flags and positional arguments can be described by a schema. From simple utilities like `ping` to tools with hundreds of flags and nested subcommands like `curl` (271 arguments) or `ffmpeg` (123 arguments). Write a JSON schema — or have an LLM generate one from the tool's documentation — and Scaffold renders a native desktop form. No custom UI code needed. What doesn't fit the model: interactive prompts that expect sequential stdin responses, full-screen TUIs that repaint the terminal, and tools whose behavior depends on stdin piping rather than flags.

### Presets

Save, name, and reload entire form configurations. Build a library of ready-to-run commands per tool. Share preset files with your team. Never reconstruct a complex command from memory again.

- **Save**: Presets > Save Preset (Ctrl+S) — enter a name and optional description
- **Load**: Presets > Preset List (Ctrl+L) — table-based picker with name, description, and last modified date
- **Edit**: Presets > Edit Preset — manage presets: edit descriptions, delete presets, toggle favorites
- **Reset**: Click "Reset to Defaults" in the command preview bar — clears everything back to schema defaults

The preset picker supports **favorites** — click the star column to pin frequently-used presets to the top. Presets are stored as JSON in `presets/{tool_name}/` and are portable. Schema changes are handled gracefully: removed fields are silently skipped, new fields get defaults.

### Syntax-colored command preview

Watch the exact command build in real time as you change fields. Tokens are color-coded by type (binary, flags, values) in both light and dark modes. Right-click the preview to copy formatted for Bash, PowerShell, or Windows CMD with shell-appropriate quoting.

### Process execution

Run commands directly from the GUI with colored output (stdout in one color, stderr in another), exit codes, and timestamps. The output panel is searchable (Ctrl+Shift+F). Copy or save output to file. ANSI escape codes are automatically stripped. Process stop uses SIGTERM first with SIGKILL fallback for clean shutdown. Optional auto-kill timeout per tool.

### 10 widget types

Each CLI argument gets the right input control: checkboxes for booleans, text fields for strings, spin boxes for integers and floats, dropdowns for enums, checkable lists for multi-enums, file and directory browsers, password fields with show/hide toggle, and multi-line text areas.

### Subcommand support

Tools like `git` with multiple subcommands work seamlessly. Multi-word subcommand chains like `ansible-galaxy role install` or `docker compose up` are supported — each word becomes a separate token in the assembled command. Selecting a subcommand swaps the visible form fields while global flags remain.

### Command history

Every executed command is automatically recorded. Browse recent runs per tool via View > Command History (Ctrl+H), see exit codes and timestamps, and restore any past form state with one click. A filter bar lets you search across entries.


### LLM-powered preset generation

Generate presets from natural language using `--preset-prompt` and `PRESET_PROMPT.txt`, mirroring the schema-generation workflow. Run `python scaffold.py --preset-prompt` to print the prompt, paste it alongside the target tool's schema into any LLM, describe the preset you want in plain English, and drop the returned JSON into `presets/<tool>/`. This is useful for tools with many flags where manually configuring a preset would be tedious.


### Auto-save and crash recovery

Form state is automatically saved 2 seconds after each change. If the app crashes, the next launch offers to restore your work. Unchanged forms are not saved. Stale recovery files are cleaned up on startup.

### Cascade chaining

Chain multiple tool runs into sequential workflows from the cascade sidebar (Ctrl+G). Each cascade holds up to 20 numbered slots, each assignable to any loaded tool and an optional preset. Configure per-step delays (0–3600 seconds) to pace the chain, enable loop mode to repeat the full sequence indefinitely, or use stop-on-error mode to halt on the first non-zero exit code. Cascade variables let you define named parameters (with type hints: `string`, `file`, `directory`, `integer`, `float`) that are prompted at run time and injected into form fields across steps — useful for target IPs, output directories, or any value that changes between runs.

**Cascade captures** let later steps reuse values extracted from earlier step output. A capture can pull data from stdout or stderr via regex, read a file path, grab an exit code, or take the output stream tail, then inject it into subsequent steps as a `{name}` substitution.

Cascade files can be saved, loaded, imported, exported, and deleted from the sidebar. State persists across sessions. Cascades run through the same QProcess list-based execution pipeline as single-tool runs — no shell, no shortcuts, same security guarantees. Six example cascade files are bundled in `cascades/` (`recon_basic`, `ping_then_nmap`, `cascade_archive_playlist`, `cascade_pandoc_dual_output`, `cascade_ssh_scp`, and `cascade_nmap_ncat`).

### Cascade history

Every cascade run is automatically recorded with its full configuration snapshot and each step's post-substitution command. Open the history dialog via View > Cascade History (Ctrl+Shift+H) to browse past runs. Restore Step reloads a single step's form state; Restore Cascade reloads the entire cascade configuration into the sidebar without auto-running it. Export saves one cascade's history to JSON; Export All saves everything. Up to 50 entries are retained (oldest evicted first). In loop mode, each iteration is a separate history entry, so loop-heavy runs fill history faster. Runs interrupted by a crash appear in history as "crashed" after the next startup.

<img src="nmap%20cascade%20example.png" alt="Scaffold cascade panel running an nmap workflow" width="75%">


### LLM-powered cascade generation (experimental)

Generating cascades with an LLM is harder than generating schemas or presets because cascades reference multiple tools — pasting several full schemas into one chat quickly exhausts usable context. The workaround: configure a Claude Project (or any LLM environment with persistent document context) that already has your `tools/` and `presets/` directories loaded, paste the provided instructions into the Project's custom instructions, and describe the cascade you want in plain English.

Generation quality is bounded by preset coverage. Presets are pre-validated flag combinations with human-written descriptions, which the LLM can reason about far more reliably than raw schemas. The instructions direct the LLM to prefer presets and fall back to full-schema flag selection only when no suitable preset exists — so the more curated your preset library, the better your generated cascades.

This feature is **experimental** and documentation-only. See `CASCADE_LLM_GENERATION_GUIDE.md` for setup, usage, and known limitations.



### Custom PATH Directories

Add directories to Scaffold's PATH so it can find and execute binaries — including scripts — that aren't on your system PATH. Access via **File > Custom PATH Directories...**. Added directories are prepended to PATH for both availability lookups (tool picker installed/not-installed markers, per-tool warning bar) and process execution (QProcess environment), so a tool resolved via a custom directory works identically to one on the system PATH. Settings persist across sessions in QSettings, or in `scaffold.ini` alongside the app in portable mode.

The main use case is script-based tools. Write a schema with `"binary": "myscript.py"`, add the script's directory to your custom PATH, and Scaffold runs the script like any other tool. Scripts must be independently executable: on Unix that means a shebang line and the execute bit set; on Windows, `.bat` and `.cmd` files work natively, and `.py` scripts work when Python is installed with `PATHEXT` configured (the default installer does this). `.ps1` PowerShell scripts can't be launched directly — use `"binary": "powershell"` and pass the script path as the first positional argument instead.

### Additional features

- **Deprecated and dangerous indicators** — schema flags can be marked `deprecated` (strikethrough + warning) or `dangerous` (red caution symbol) for visual heads-up without disabling them
- **Min/max constraints** — optional `min` and `max` on integer/float arguments constrain the spinner range
- **Drag and drop** — drop a `.json` schema file onto the window to load it
- **Help menu** — About dialog with version info and GitHub link, cascade guide, keyboard shortcuts reference
- **Session persistence** — remembers window size, position, theme, and last opened tool
- **Format markers** — `_format` metadata key prevents accidentally loading presets as tool schemas or vice versa, with clear error messages
- **Bundled example schema** — `tools/example.json` demonstrates every feature in one file
- **Portable mode** — place `portable.txt` next to `scaffold.py` to store all settings in a local INI file instead of the system registry. Run from a USB drive with fully isolated configuration
- **Bundled tool schemas** — 28 schemas covering aircrack-ng, airodump-ng, ansible (4 tools), curl, docker (3 tools), ffmpeg, find, git, gobuster, hashcat, ncat, nikto, nmap, openclaw, ping, rsync, scp, ssh, tar, and wget. Some are mostly untested — contributions and bug reports welcome. Several ship with bundled presets under `default_presets/` (seeded into your local `presets/` folder on first open) — currently `find`, `ncat`, `nmap`, `rsync`, `scp`, `ssh`, `tar`, and `wget`
- **Experimental schemas** — `tools/pandoc.json` and `tools/yt-dlp.json` are newly added and experimental. Bundled example presets live under `default_presets/pandoc/` and `default_presets/yt-dlp/` (seeded into your local `presets/` folder on first open), and example cascades live at `cascades/cascade_pandoc_dual_output.json` and `cascades/cascade_archive_playlist.json`. Expect rough edges — please report any issues you find

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| **Enter** | Run the command (when not typing in a field) |
| **Shift+Enter** | Run cascade chain (when not typing in a field) |
| **Ctrl+Enter** | Run the command |
| **Escape** | Stop a running process |
| **Ctrl+S** | Save preset |
| **Ctrl+L** | Preset list |
| **Ctrl+H** | Command history |
| **Ctrl+Shift+H** | Cascade history |
| **Ctrl+O** | Load tool from file |
| **Ctrl+R** | Reload current tool |
| **Ctrl+F** | Focus field search bar |
| **Ctrl+Shift+F** | Search within output panel |
| **Ctrl+D** | Toggle dark mode |
| **Ctrl+G** | Toggle cascade panel |
| **Ctrl+B** | Tool list |
| **Ctrl+Q** | Quit |

## Command-Line Usage

```bash
# Launch the GUI (tool picker)
python scaffold.py

# Launch directly into a specific tool
python scaffold.py tools/nmap.json

# Validate a schema without launching the GUI
python scaffold.py --validate tools/mytool.json

# Print the LLM prompt for schema generation
python scaffold.py --prompt

# Print the LLM prompt for preset generation
python scaffold.py --preset-prompt

# Show version and exit
python scaffold.py --version
```

---

## Limitations

- **Scaffold does not auto-generate schemas.** You write the JSON (or have an LLM generate it from documentation). There is no automatic flag detection from a binary.
- **Scaffold does not sandbox the target binary.** The security model prevents shell injection and constrains input, but once a command runs, the target program has whatever permissions the user has.
- **Schema generation quality depends on the LLM and the documentation.** `--help` output alone is usually not detailed enough. Man pages or official docs produce much better schemas. Very large tools may exceed an LLM's context window.
- **Tested primarily on Windows.** PySide6 is cross-platform and Scaffold should work on macOS and Linux, but those platforms have had less testing. If you hit platform-specific issues, please open an issue.
- **Requires PySide6.** There are no plans to support other GUI frameworks.
- **Large tools with deeply nested subcommand trees** (70+ subcommands, 200+ arguments) produce usable but dense forms. Scaffold helps with discoverability and prevents syntax errors, but for very large tools it's more of a reference than a streamlined workflow.

---

## About This Project

Much of the code in Scaffold was written by [Claude Code](https://claude.ai) (Opus 4.6) with edits from the author — but this was a collaboration, not delegation. The project was designed, directed, tested, and iterated by a human who audited every line of code and every command, and made direct edits where needed. This wasn't a one-shot generation, and took very careful planning and execution.

Scaffold was built the way a real team would build software, just with an AI writing most of the code:

1. **Architecture first** — started with a design document defining the widget type system, schema format, and command assembly pipeline before any code was written
2. **Staged deliverables** — the project was built in phases: core engine, widget rendering, command execution, presets, subcommands, dark mode, elevated execution, UI polish, schema generation prompt
3. **Tests alongside features** — test cases were planned with each stage, not bolted on after. The test suites (3,798 assertions across 6 suites) were written to validate each feature as it was delivered
4. **Code review cycles** — after the core was stable, the codebase went through multi-part review: cleanup, error handling audit, performance profiling, and linting
5. **Iteration, not generation** — most features took multiple rounds of build-test-fix. The dark mode scrollbar fix alone went through QSS, QProxyStyle, and finally native `setColorScheme` before it worked correctly
6. **Manual QA on every release** — every version was tested by hand on real tools before tagging

The author has 15 years of professional IT experience and holds certifications in cybersecurity, ethical hacking, penetration testing, and Python development — not a software developer by trade, but far from starting from zero. Claude Code is a useful tool, but a tool still needs someone behind it who knows what they're building and why.

**Methodology: plain-English bug hunting.** Most bugs in recent releases were found the same way: first I describe in as much detail as possible what the bug is either in the logic or in the UI. Then we write a focused diagnostic prompt describing the suspected issue, run it against the full codebase in a fresh context window, collect measurements and code traces, then write a targeted fix grounded in confirmed findings. No fix prompt is written from assumption — every non-obvious bug gets a diagnostic pass first before writing any fixes.

**Multi-model distillation.** External audits from Copilot, ChatGPT, and other models surface candidate findings, which are fed back into Claude Opus Extended Thinking for architectural review and final judgment on what's real, what's noise, and what's over-engineering.

---

## Support the Project

This project was built as a hobby by one person, a couple computers, and a couple of LLMs. It burned through quite a bit of token cost and mass amounts of personal time — but it was worth it. If Scaffold saves you some time or you just think it's cool, consider supporting ongoing development.

- **GitHub Sponsors** — sponsor via [github.com/sponsors/kevwillow](https://github.com/sponsors/kevwillow)
- **Star this repo** — it's free and it helps others find the project
- **Share your tool schemas** — submit a PR to add schemas for popular CLI tools
- **Crypto donations** — if you're feeling generous:
  - **BTC** — `bc1qmtzjlc2cw2y45nea2jqf4deh946j8mq502zvsw`
  - **BTC (Unstoppable Domain)** — `gurutech.blockchain`
  - **LTC** — `ltc1qf32n038a90ulajlq6zz67r3n2myewpjlj2ej6w`
  - **ETH** — `0x9bf3311c4721fe37f58913dc57c2bf1722dc8a0f`
  - **BCH** — `bitcoincash:qr2l294kuve9cw48u7xek9nklhed066ycvjtj4ymq9`
  - **SOL** — `CuraE8usMpSrAhpY2QiWaQGoBjyJzkSaUNP6kRgAzscU`

- **Contact** — kev@gurutechnology.services

## License

Licensed under the GNU Affero General Public License v3.0 — see [LICENSE](LICENSE). Copyright (C) 2026 Kev Wilson.
