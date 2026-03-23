# O.R.A. — Terminal User Interface Specification v0.1

### O.R.A. — Orchestrated Reasoning Agent

> Defines the three-panel Textual TUI that replaces the single-stream CLI as the
> default interaction mode. The classic CLI remains available via `--cli`.

---

## Overview

The TUI provides a split-panel terminal interface built on
[Textual](https://github.com/Textualize/textual). It separates the model's
internal reasoning from the conversation output and gives the user a built-in
file editor for workspace configuration — all without leaving the terminal.

The classic CLI (`python main.py`) is preserved as a fallback for minimal
environments or headless use.

---

## Layout

```
+--------------------+----------------------------+--------------------+
|  Thinking & Tools  |       Conversation         |     Settings       |
|                    |                            |   (hidden until    |
|  [magenta thinking |  > user message            |    /settings)      |
|   stream]          |  Ora: streamed response    |                    |
|                    |                            |  [directory tree]  |
|  > tool: run_bash  |  > user message            |  [file editor]    |
|    ls ~/Desktop    |  Ora: streamed response    |  [Save] [Close]   |
|  < show_paths: ... |                            |                    |
+--------------------+----------------------------+--------------------+
| Model: qwen3:4b | Workspace: ~/.local/share/ora-os | Config: ...     |
+-----------------------------------------------------------------------+
```

| Panel | Width | Purpose |
|-------|-------|---------|
| Left — Thinking & Tools | 30 cols fixed | Model chain-of-thought (streamed live), tool call names + args, tool results (truncated) |
| Center — Conversation | Flexible (fills remaining) | User messages, Ora responses (streamed token-by-token), system messages |
| Right — Settings | 38 cols fixed, hidden by default | DirectoryTree of workspace, TextArea file editor, Save/Close buttons |
| Status bar | Full width, 1 line | Active model, workspace path, config pointer path |

---

## Panel details

### Left — Thinking & Tools

When the model produces thinking tokens (via Ollama's `think=true`), they stream
into this panel in real time, styled as dim italic magenta text inside a bordered
block:

```
──── thought ────
The user wants to see files on their desktop.
I should use run_bash with ls ~/Desktop.
────────────────
```

Tool calls appear below thinking blocks:

```
> run_bash
  ls ~/Desktop
< show_paths: O.R.A. file locations: ...
```

- `run_bash` shows only the tool name (the command appears in the confirmation modal)
- Other tools show name + truncated args (80 char limit)
- Tool results show name + truncated output (200 char limit)
- `run_bash` results are not duplicated here (the modal + output already show them)

### Center — Conversation

Standard chat display:

- **User messages**: cyan `>` prefix
- **Ora responses**: bold `Ora:` prefix, streamed token-by-token via `Static.update()`
- **System messages**: dim italic (settings opened, config reloaded, errors)

The input bar is docked at the bottom. It is disabled while the agent is
processing a turn to prevent double-submission.

### Right — Settings

Hidden by default. Appears when the user types `/settings`. Contains:

1. **DirectoryTree** — shows all files in the workspace directory
2. **File path label** — name of the currently open file
3. **TextArea** — full editor with markdown syntax highlighting
4. **Save button** (or Ctrl+S) — writes the file to disk; if the file is
   `config.md`, the config is automatically reloaded
5. **Close button** — hides the panel and reloads config

The user can edit files directly without asking the model. The model-assisted
`/settings` conversational mode from the CLI is replaced by this direct editor
in TUI mode.

Closing the panel (`/done`, `/close`, or Close button) reloads `config.md`
so any changes take effect immediately.

---

## Startup sequence

1. **CLI-based setup** runs first (before Textual launches):
   - Workspace resolution
   - First-run wizard (if needed)
   - Hardware probe
   - Network scan + trust decisions
   - Model selection
2. **`setup_session()`** returns a dict with all session state
3. **`OraApp(session)`** is created and `.run()` launches the TUI
4. **`on_mount()`** builds the agent graph (tools, LLM, LangGraph) — this
   requires `self` for `call_from_thread` callbacks

The wizard and model selection prompts use Rich's CLI `Prompt.ask()`, which
cannot run inside Textual. This is why setup happens before the TUI starts.

---

## Agent integration

### Graph building

The TUI builds its own LangGraph (`_build_graph`) with nodes that call
`app.call_from_thread()` to update widgets:

| Node | Callback | Target panel |
|------|----------|-------------|
| `call_llm` — thinking chunk | `_ui_start_thinking`, `_ui_append_thinking`, `_ui_finish_thinking` | Left |
| `call_llm` — content chunk | `_ui_start_response`, `_ui_append_response`, `_ui_finish_response` | Center |
| `call_llm` — tool_calls | `_ui_show_tool_call` | Left |
| `call_tools` — result | `_ui_show_tool_result` | Left |

### Worker thread

Each agent turn runs in a Textual `@work(exclusive=True, thread=True)` worker.
The graph's `call_llm` and `call_tools` nodes execute in this thread and use
`call_from_thread()` to post UI updates to the main event loop.

### Bash confirmation

When `run_bash` needs user confirmation:

1. The worker thread calls `_request_confirm(command, is_destructive)`
2. This schedules a `ConfirmScreen` modal on the main event loop via
   `asyncio.run_coroutine_threadsafe`
3. The worker blocks on `future.result()` until the user presses y/n
4. The modal dismisses with `True` or `False`, unblocking the worker

The `ConfirmScreen` binds `y` → confirm, `n`/`Esc` → deny. Destructive
commands show a red warning banner.

---

## Keybindings

| Key | Action |
|-----|--------|
| `Enter` | Submit message (in input bar) |
| `Ctrl+S` | Save current file in settings editor |
| `Ctrl+Q` | Quit |
| `y` | Confirm (in bash confirmation modal) |
| `n` / `Esc` | Deny (in bash confirmation modal) |

---

## Commands

Typed in the input bar:

| Command | Action |
|---------|--------|
| `/settings` | Open the settings panel |
| `/done` or `/close` | Close settings panel, reload config |
| `exit` / `quit` / `bye` | Save session and exit |

---

## Fallback: Classic CLI

```bash
./run.sh --cli     # or: python main.py
```

The CLI mode uses the original single-stream output with `ThinkingStreamPrinter`
for ANSI-styled thinking blocks and Rich console for everything else. It is
useful for:

- SSH sessions with limited terminal capabilities
- Headless / scripted usage
- Debugging (simpler output to parse)

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `textual` | >= 1.0.0 | TUI framework (layout, widgets, events, async workers) |
| `rich` | >= 13.0.0 | Markup rendering inside Textual widgets (already a dependency) |

Textual is built by the same team as Rich and shares its markup language, so
existing Rich renderables work inside Textual widgets without changes.

---

## File structure

```
tui.py
├── ConfirmScreen      — Modal for bash command confirmation
└── OraApp             — Main Textual App
    ├── compose()      — Three-panel layout
    ├── on_mount()     — Event loop ref, agent build, focus
    ├── _build_agent() — Tools, LLM, graph construction
    ├── _build_graph() — LangGraph with TUI streaming callbacks
    ├── _ui_*()        — Widget update methods (thinking, response, tools)
    ├── _request_confirm() — Thread-safe bash confirmation bridge
    ├── on_input_submitted() — Input routing (/settings, /done, agent turn)
    ├── _run_agent_turn()   — @work background agent execution
    ├── on_file_selected()  — Settings: load file into editor
    └── action_save_file()  — Settings: write file to disk
```
