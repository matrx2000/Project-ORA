# Project O.R.A.

**O.R.A. — Orchestrated Reasoning Agent**

O.R.A. is a locally-hosted, self-bootstrapping agentic operating system. Linux is the base
layer — managing hardware, files, and processes — and a fleet of local LLMs served by
[Ollama](https://ollama.com) is the intelligence layer. The agent can reason, switch
specialist models on demand, see images, manage its own context, and operate the underlying
Linux system autonomously.

Everything runs on your machine. No cloud APIs. No data leaves the box.

> **WARNING** — O.R.A. has unrestricted access to the Linux filesystem and can install
> packages, manage processes, and modify system state. **Run it on a dedicated machine
> that does not contain personal data.** Do not run it on your daily driver.
>
> **Do not install or use this software if you don't understand and regularly use terms like:**
>
> **Linux & systems:**
> `sudo`, `root`, `systemctl`, `ssh`, `chmod`, `kill -9`, `cron`, `daemon`, `PID`,
> `iptables`, `/etc/fstab`, `apt`, `dd`, `partition table`, `port forwarding`,
> `.bashrc`, `environment variables`
>
> **AI & LLMs:**
> `LLM`, `inference`, `VRAM`, `quantization`, `context window`, `tokens`,
> `hallucination`, `prompt injection`, `model parameters`, `temperature`,
> `system prompt`
>
> **Privacy & security:**
> `PII`, `doxing`, `credential leakage`, `attack surface`, `exfiltration`,
> `network exposure`, `plaintext secrets`, `log sanitization`
>
> **Why this matters:**
>
> - **LLMs hallucinate.** A model can confidently propose a command that looks correct
>   but destroys data. You need to be able to read every command and judge it yourself
>   before pressing `y`.
> - **LLMs don't understand privacy.** If your system contains personal data, API keys,
>   or credentials, a model might read them, echo them in a response, or write them to
>   a session log in plain text — without knowing it did anything wrong.
> - **Self-doxing is real.** Ora reads your filesystem and writes session logs. If you
>   run it on a machine with personal files and later share those logs, workspace files,
>   or even your config, you could expose your real name, IP addresses, directory
>   structure, hostnames, SSH keys, or browser history without realizing it.
> - **Prompt injection is real.** If Ora reads a file or web content that contains hidden
>   instructions, the model might follow those instructions instead of yours. You need to
>   recognize when that's happening.
> - **Local doesn't mean safe.** No data leaves your machine, but the agent still has full
>   access to everything on it. A local model with `sudo` access can do just as much
>   damage as a remote attacker.
>
> **This tool is built for system administrators and developers who already manage Linux
> machines and understand how LLMs behave.** If any of the terms above are unfamiliar,
> learn them first — or experiment in a virtual machine where nothing important is at risk.

---

## Goals

- **Fully autonomous local agent** — perceives hardware, selects models, persists memory,
  and acts on the OS without human hand-holding.
- **Multi-model routing** — automatically delegates sub-tasks to specialist models
  (reasoning, coding, vision, fast) and returns results seamlessly.
- **Vision support** — images are routed through a two-stage pipeline: a vision model
  describes the image, then the instruct model reasons and acts on the description.
- **Network-aware** — discovers and uses Ollama instances on other machines on your
  network, with per-session user approval and trust management.
- **Human-readable state** — all configuration, memory, and session state is stored as
  plain markdown files you can open and edit in any text editor.
- **Safe by default** — every shell command requires confirmation, destructive commands
  are flagged, and dangerous patterns are hard-blocked.

---

## Features

### Agent Loop
- LangGraph ReAct loop with tool use (bash, model switching, model pulling)
- Transparent context overflow detection — when the context fills up, older messages are
  summarised automatically and the session continues without interruption
- Session summaries persist across restarts so the agent remembers what happened last time

### Terminal UI (Textual)
- Three-panel layout: **Thinking & Tools** (left) | **Conversation** (center) | **Settings** (right)
- Model thinking/reasoning streamed in real time to the left panel
- Tool calls and results visible as they happen
- Built-in file editor for workspace files — open with `/settings`, edit, and save directly
- Bash command confirmation via modal dialog
- Classic CLI mode available with `./run.sh --cli`

### Model Switching
- Role-based routing: `reasoning`, `coding`, `fast`, `vision`, `instruct`
- The agent decides when to delegate to a specialist and writes a compact context transfer
- Specialist models unload immediately after the call (Ollama TTL = 0s)

### Vision Pipeline
- Detects image file paths in your messages (`.png`, `.jpg`, `.webp`, etc.)
- Two strategies: **describe-then-reason** (default) or **vision-handles-all**
- Text files (`.py`, `.log`, `.md`, etc.) are read and injected inline — no vision model needed
- Graceful fallback when no vision model is configured

### Network Model Discovery
- Define remote Ollama nodes in `workspace/network_config.md`
- On every startup, Ora scans all nodes, inventories their models, and scores them against local options
- Better remote models are suggested to the user — never used silently
- Trust decisions can be remembered across sessions or revoked at any time
- Remote models generate text only — tool execution always stays local

### Settings Mode
- Type `/settings` mid-session to enter a conversational configuration assistant
- Read and modify any workspace file through natural language
- Every change shows a diff and requires explicit confirmation before writing
- Focus shortcuts: `/settings network`, `/settings models`, `/settings profile`, `/settings safety`

### Safety
- Every bash command requires manual `[y/N]` confirmation
- Destructive commands (`rm`, `kill`, `apt remove`) get a `[DESTRUCTIVE]` warning tag
- Hard-blocked patterns (`rm -rf /`, `mkfs`, `shutdown`, fork bombs) are refused unconditionally
- Configurable blocklist in `workspace/config.md`

---

## Requirements

- **OS:** Linux (Ubuntu 22.04+ recommended, any systemd-based distro)
- **Python:** 3.11+
- **Ollama:** installed and running locally ([install guide](https://ollama.com/download))
- **Hardware:** NVIDIA GPU (CUDA), AMD GPU (ROCm), Apple Silicon (Metal), or CPU fallback

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/matrx2000/Project-ORA.git
cd Project-ORA
chmod +x install.sh
./install.sh
```

The install script will:
- Check for Python 3.11+ and Ollama
- Create a `.venv` virtual environment
- Install all Python dependencies
- Verify key imports work
- Print next steps

If you prefer to set up manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start Ollama and pull at least one model

```bash
ollama serve              # if not already running
ollama pull qwen3:4b      # or any model you want to use
```

### 3. Launch

```bash
./run.sh          # TUI mode (three-panel interface)
./run.sh --cli    # Classic terminal mode (single stream)
```

Or manually:

```bash
source .venv/bin/activate
python tui.py     # TUI mode
python main.py    # Classic CLI mode
```

### 4. Uninstall

To completely remove O.R.A. (venv, workspace data, settings, memory):

```bash
chmod +x uninstall.sh
./uninstall.sh
```

This requires you to type `DELETE EVERYTHING` to confirm. Source code is kept.

---

## First-Run Setup

On first launch, Ora runs an interactive **Wakeup Wizard** that walks you through
the entire initial configuration. Here is what to expect:

### Step 0 — Safety Acknowledgement

You will see the full safety warning. You must type exactly `I UNDERSTAND` to proceed.
This is non-negotiable — Ora has real access to your system.

### Step 1 — Workspace Location

Ora asks where to store its configuration and memory files. The default is your OS
user-data directory (`~/.local/share/ora-os/` on Linux, `%LOCALAPPDATA%\OraOS\ora-os\`
on Windows), which keeps private files out of any git repository.

You can accept the default or enter a custom path. If the chosen path is inside a git
repository and not covered by `.gitignore`, Ora will warn you and offer to add it
automatically — it will not proceed until the path is safe.

### Step 2 — Ollama Scan

Ora connects to your local Ollama instance and lists all models you have already pulled.
If Ollama is not running or no models are found, you can still continue — models can be
configured manually and pulled later.

### Step 3 — Bootstrap Model

You pick a small model that is already pulled in Ollama (e.g. `qwen3:4b` or `phi4-mini`).
This model runs the wizard itself — it needs to be available right now.

### Step 4 — Hardware Detection

Ora probes your CPU, RAM, and GPU (NVIDIA via `pynvml`, AMD via `rocm-smi`, Apple Silicon
via `system_profiler`). The results are written to `hardware_profile.md` in the workspace
and used to determine which models fit in your VRAM/RAM.

### Step 5 — Hardware Tier / Model Configuration

This is where you set up your model fleet. You have two options:

**Option A — Pick a preset:**

| Tier | Target Hardware | Models |
|------|----------------|--------|
| 1 | Jetson / Low-end (<=8GB) | mistral-small3.1:24b-24q4_K_M, qwen2.5-vl:3b, deepseek-r1:1.5b, phi4-mini:3.8b |
| 2 | Mid-range (RTX 3080 ~10GB) | qwen3:8b, qwen2.5-vl:7b, deepseek-r1:7b, qwen3:4b |
| 3 | High-end (RTX 4090 ~24GB) | qwen3-coder:30b, qwen2.5-vl:7b, deepseek-r1:14b, qwen3:4b |

Each preset configures an instruct model, a vision model, a reasoning model, and a fast
model — with appropriate sizes for your VRAM budget.

**Option B — Custom configuration:**

Add models one by one. For each model you enter:
- **Name** — copy-paste from [ollama.com/library](https://ollama.com/library) (e.g. `qwen3:4b`)
- **Size** — estimated size in GB (auto-detected if already pulled)
- **Role** — `instruct`, `reasoning`, `coding`, `fast`, `vision`, or `general`
- **Capabilities** — `text` for text-only models, `text,images` for vision/multimodal models
- **Description** — what the model is good at (helps Ora route tasks correctly)
- **Auto-pull** — whether Ora can download this model automatically if it is not yet pulled

### Step 6 — User Profile

The bootstrap model guides you through a short conversation to set up your user profile
(name, working style, current projects). This is injected into every system prompt so
Ora can tailor its responses.

### Done

All configuration is written to the workspace directory as plain markdown files.
You can edit any of them at any time — between sessions or even during a session with
`/settings`. Ora then launches into its main agent loop.

---

## Project Structure

```
Project-ORA/
|-- tui.py                   # TUI entry point: three-panel Textual interface
|-- main.py                  # CLI entry point: classic terminal mode, shared setup logic
|-- boot.py                  # First-run wakeup wizard with tier presets
|-- bash_tool.py             # Restricted shell execution with confirmation
|-- requirements.txt         # Python dependencies
|
|-- tools/
|   |-- hardware_probe.py    # CPU/RAM/GPU detection, model fit scoring
|   |-- ollama_manager.py    # list_models() and pull_model() tools
|   |-- model_switcher.py    # Role-based model delegation (local + remote)
|   |-- context_manager.py   # Token counting, overflow detection, summarisation
|   |-- network_scanner.py   # Remote Ollama node discovery and trust
|   |-- vision_router.py     # Image/file detection, two-stage vision pipeline
|   |-- workspace_resolver.py # Locates workspace via platformdirs, git safety checks
|
|-- workspace/               # All persistent state (lives outside repo by default, see below)
|   |-- config.md            # Main agent configuration
|   |-- user_profile.md      # User name, preferences, projects
|   |-- hardware_profile.md  # Auto-generated hardware snapshot
|   |-- viable_models.md     # Allowed models with capabilities and fit scores
|   |-- model_roles.md       # Role-to-model assignments
|   |-- vision_config.md     # Vision routing settings
|   |-- session_state.md     # Live session: active model, token usage, logs
|   |-- network_config.md    # Remote Ollama nodes (user-created)
|   |-- network_registry.md  # Auto-generated network scan results
|   |-- network_trust.md     # Remembered trust decisions
|   |-- memory/
|       |-- context_summary.md    # Rolling session summary
|       |-- persistent_memory.md  # Long-term facts across sessions
|
|-- specs/                   # Design specifications
|   |-- project_spec.md      # Core architecture, boot sequence, agent loop
|   |-- network_spec.md      # Remote Ollama discovery and trust system
|   |-- settings_spec.md     # /settings conversational configuration mode
|   |-- multimodal_spec.md   # Vision pipeline, hardware tier presets
|   |-- workspace_location_spec.md  # Workspace location, platformdirs, git safety
|   |-- tui_spec.md             # Three-panel Textual TUI layout and architecture
```

---

## Specifications

The design of every subsystem is documented in detail in the `specs/` directory:

| Spec | What it covers |
|------|---------------|
| [project_spec.md](specs/project_spec.md) | Core architecture, boot sequence, agent loop, system prompt template, tool definitions, workspace file formats, success criteria |
| [network_spec.md](specs/network_spec.md) | Remote Ollama node discovery, model scoring vs local, per-session trust approvals, remote model system prompt restrictions, offline fallback |
| [settings_spec.md](specs/settings_spec.md) | `/settings` conversational configuration mode, diff-and-confirm workflow, what can and cannot be changed mid-session |
| [multimodal_spec.md](specs/multimodal_spec.md) | Vision routing pipeline, two-stage describe-then-reason strategy, capabilities column in viable_models.md, hardware tier presets, graceful failure cases |
| [workspace_location_spec.md](specs/workspace_location_spec.md) | Workspace stored outside the repo via `platformdirs`, git safety checks, boot wizard workspace selection, `workspace.conf` pointer file |
| [tui_spec.md](specs/tui_spec.md) | Three-panel Textual TUI layout, streaming thinking/response to separate panels, settings file editor, bash confirmation modal, async agent worker, keybindings |

These specs are the source of truth for how each feature is designed and should behave.
If you want to understand why something works the way it does, start here.

---

## Usage

Once Ora is running, you interact through a terminal prompt:

```
> what services are failing on this machine?
Ora: Let me check. [calls run_bash("systemctl --failed")]
...

> /settings
[ora/settings] Settings mode active. What would you like to change?

> add a new remote node called gpu-box at 192.168.1.50:11434
[ora/settings] I'll add gpu-box to network_config.md...

> /done
[ora] Returning to normal mode.

> look at /tmp/screenshot.png and tell me what's wrong
[ora] Reading image... done. Reasoning...
Ora: The screenshot shows a systemd service failure with exit code 203...
```

**Commands:**
- `exit` / `quit` / `Ctrl+C` — save session summary and exit
- `/settings` — enter settings mode
- `/settings network` — settings mode focused on network config
- `/done` — exit settings mode

---

## Configuration Reference

All settings live in `config.md` inside your workspace directory. You can edit
them manually or use `/settings` mid-session. Run `show_paths` to find where
your workspace is stored.

### Ollama

| Setting | Default | Description |
|---------|---------|-------------|
| `base_url` | `http://127.0.0.1:11434` | Ollama API endpoint |

### Models

| Setting | Default | Description |
|---------|---------|-------------|
| `bootstrap_model` | `phi4-mini` | Model used during the first-run wizard |
| `default_model` | *(blank)* | Model for the main agent loop. Leave blank to be prompted at startup |
| `allow_agent_initiated_switching` | `true` | Allow the agent to switch to specialist models on its own |
| `require_user_confirm_switch` | `false` | Require `[y/N]` confirmation before every model switch |

### Context Overflow

| Setting | Default | Description |
|---------|---------|-------------|
| `overflow_threshold` | `0.82` | Context usage percentage that triggers automatic summarisation (0.0–1.0) |
| `summary_keep_last_n_turns` | `4` | Number of recent turns kept verbatim after summarisation |
| `max_summary_tokens` | `400` | Maximum token length for the compressed summary |

### Safety

| Setting | Default | Description |
|---------|---------|-------------|
| `bash_require_confirm` | `true` | Require `[y/N]` confirmation before every bash command. Set to `false` to auto-execute. |
| `bash_restrict_to_workspace` | `true` | When enabled, commands that target paths outside the workspace directory are blocked. Disable to give the agent full Linux filesystem access. |
| `bash_warn_destructive` | `true` | Show `[DESTRUCTIVE]` tag on dangerous commands (`rm`, `kill`, etc.). When disabled, these commands run without the extra warning. **Hard-blocked patterns are always enforced regardless.** |
| `bash_exclude_commands` | `rm -rf /,mkfs,dd if=/dev/zero,shutdown,reboot` | Comma-separated list of hard-blocked command patterns. These are refused unconditionally and cannot be toggled off. |

### Session

| Setting | Default | Description |
|---------|---------|-------------|
| `auto_save_session_state` | `true` | Write `session_state.md` after every turn |
| `auto_reload_config` | `false` | Re-read `config.md` at the start of every turn (useful if you edit config mid-session in another editor) |

### Changing settings mid-session

Type `/settings` to enter settings mode, then describe what you want to change
in plain language:

```
> /settings safety
> turn off bash confirmation and give me full filesystem access
ora: I'll make these changes to config.md:
     - bash_require_confirm: true -> false
     - bash_restrict_to_workspace: true -> false
     Confirm? [y/N]:
```

Focused shortcuts: `/settings network`, `/settings models`, `/settings profile`,
`/settings safety`, `/settings memory`, `/settings vision`.

Type `/done` to return to normal mode.

---

## License

[MIT](LICENSE)
