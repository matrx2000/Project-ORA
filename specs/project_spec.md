# Ora OS — Project Specification v0.1
### O.R.A. — Orchestrated Reasoning Agent

## Vision

**Ora OS** is a locally-hosted, self-bootstrapping agentic operating system where Linux is
the base layer — managing hardware, files, and processes — and a fleet of local LLMs (served
by Ollama) is the intelligence layer. The agent can reason, switch specialist models on
demand, manage its own context, and operate the underlying Linux system autonomously.

The long-term goal is a fully autonomous agentic OS that perceives its hardware environment,
manages its own model selection, persists memory across sessions, and exposes itself remotely.
**v1 is the foundation**: terminal-first, single machine, local models only.

> ⚠️ **SAFETY WARNING — displayed prominently on every startup:**
> Ora OS has unrestricted access to the Linux filesystem and can install packages, manage
> processes, and modify system state. **Run it on a dedicated machine that does not contain
> personal data.** Do not run it on your daily driver. You have been warned.

---

## Target Platform

- **OS:** Linux (Ubuntu 22.04+ recommended; any systemd-based distro supported)
- **Primary language:** Python 3.11+
- **Interface (v1):** Terminal / shell — accessible locally or via remote shell (e.g. Tailscale SSH)
- **Hardware acceleration:** NVIDIA GPU (CUDA), AMD GPU (ROCm), Apple Silicon (Metal), or CPU
  fallback — whatever Ollama supports on the host machine
- **Model runtime:** Ollama (local, no cloud APIs)

**Future interface iterations (out of scope for v1, noted for roadmap):**
- Web UI (browser-based chat/dashboard)
- REST API (so other machines or scripts can send tasks to Ora OS)
- Mobile app interface
- Multi-machine / cluster mode

---

## Repository Structure

```
ora_os/
│
├── main.py                        # Entry point — boot sequence + LangGraph ReAct loop
├── boot.py                        # First-run wakeup wizard
├── bash_tool.py                   # Restricted Linux shell execution layer
├── requirements.txt
│
├── tools/
│   ├── __init__.py
│   ├── hardware_probe.py          # Detect RAM, VRAM, GPU backend
│   ├── model_switcher.py          # Delegate sub-tasks to specialist models
│   ├── context_manager.py         # Monitor token usage, summarise on overflow
│   └── ollama_manager.py          # Pull models, list models, check load feasibility
│
└── workspace/                     # All persistent state — plain markdown, human-editable
    ├── config.md                  # User-editable agent settings
    ├── user_profile.md            # Persistent user profile (name, preferences, projects)
    ├── hardware_profile.md        # Auto-written on boot
    ├── viable_models.md           # The "allowed models" list — editable by user or agent
    ├── model_roles.md             # User-assigned roles and descriptions per model
    ├── session_state.md           # Live session state (current model, token usage, switches)
    └── memory/
        ├── context_summary.md     # Rolling compressed conversation summary
        └── persistent_memory.md  # Long-term facts that survive across sessions
```

---

## Boot Sequence

### First Run — Wakeup Wizard (`boot.py`)

On first launch (detected by absence of `workspace/config.md`), Ora OS runs an interactive
terminal wizard that:

1. Displays the **safety warning** in full and requires the user to type `I UNDERSTAND` to
   proceed.
2. Asks for a **bootstrap model** — the minimal model used to run the wizard itself (e.g.
   `phi4-mini`). This model must already be pulled in Ollama or small enough to pull quickly.
   Written to `workspace/config.md` as `bootstrap_model`.
3. Runs `hardware_probe.py` and writes `workspace/hardware_profile.md`.
4. Runs `ollama_manager.py` to list all locally pulled models, scores them against hardware,
   and writes an initial `workspace/viable_models.md`.
5. Using the bootstrap model, opens an **interactive prompt session** where the user (and the
   model) can jointly populate:
   - `workspace/user_profile.md` (user name, working style, current projects)
   - `workspace/model_roles.md` (which model to use for which role)
   - `workspace/viable_models.md` (confirm or extend the initial model list)
6. Writes `workspace/config.md` with all settings.
7. Exits the wizard and launches the main agent loop.

### Subsequent Runs (`main.py`)

1. Display safety warning (brief, one-liner).
2. Load `workspace/config.md`.
3. Run `hardware_probe.py` (refresh hardware profile — VRAM availability changes between runs).
4. Load `workspace/viable_models.md` and `workspace/model_roles.md`.
5. Load `workspace/user_profile.md` and `workspace/memory/persistent_memory.md`.
6. Load `workspace/memory/context_summary.md` (previous session summary, injected into
   system prompt).
7. Select active model from `config.md` → `default_model` (or prompt if blank).
8. Enter LangGraph ReAct loop.

---

## Markdown Files

All files live in `workspace/`. The agent reads and writes them via plain file I/O.
The user can open and edit any of them at any time between sessions — or even during a
session if `auto_reload_config: true`.

---

### `workspace/config.md`

User-editable. Read on every boot. Can be edited before launch to change bootstrap model
or default model without entering the wizard again.

```markdown
# Ora OS Config

## Ollama
base_url: http://127.0.0.1:11434

## Bootstrap model (used during first-run wizard)
bootstrap_model: phi4-mini

## Default model for main agent loop
## Leave blank to prompt at startup
default_model: qwen3-coder:30b

## Context overflow
overflow_threshold: 0.82
summary_keep_last_n_turns: 4
max_summary_tokens: 400

## Model switching
allow_agent_initiated_switching: true
require_user_confirm_switch: false

## Safety
bash_exclude_commands: rm -rf /,mkfs,dd if=/dev/zero,shutdown,reboot
bash_require_confirm: true

## Session
auto_save_session_state: true
auto_reload_config: false
workspace_dir: ./workspace
```

---

### `workspace/viable_models.md`

The master list of models Ora OS is permitted to use. Written initially by the wakeup
wizard; editable by the user at any time; the agent can also append entries if it determines
a new model would be beneficial (subject to hardware fit check).

If a model in this list is not currently pulled in Ollama, the agent may pull it
automatically — but only if the model fits within available VRAM/RAM and `auto_pull: yes`.

```markdown
# Viable Models

## Format
Each entry: model name | estimated size | role tag | notes | auto-pull allowed

| model                    | size_gb | role        | notes                                      | auto_pull |
|--------------------------|---------|-------------|--------------------------------------------|-----------|
| phi4-mini:latest         | 2.5     | bootstrap   | used during first-run wizard               | yes       |
| qwen3-coder:30b          | 18.5    | coding      | best for code generation and bash tasks    | yes       |
| deepseek-r1:14b          | 9.0     | reasoning   | use for logic, maths, planning             | yes       |
| qwen3:4b-instruct        | 2.5     | fast        | quick lookups, low-stakes tasks            | yes       |
| devstral:latest          | 14.0    | coding      | alternative coder, good at tool use        | no        |

## Notes
- Models not in this list cannot be loaded by the agent.
- Set auto_pull: no to prevent the agent from downloading a model without explicit user action.
- The agent will never load a model that does not fit in available VRAM+RAM.
```

---

### `workspace/model_roles.md`

User-defined role assignments with natural language descriptions that are injected directly
into the system prompt. This is how the agent learns *when* to switch and *to which* model.

```markdown
# Model Roles

## Instructions to agent
When you determine that a sub-task requires a specialist capability, consult this file
to choose the correct model. Always prefer models marked as fits_vram: true.

## Role definitions

### reasoning
model: deepseek-r1:14b
use_when: >
  You need to reason through a logical problem, evaluate trade-offs, solve a mathematical
  challenge, or plan a multi-step approach before acting. Do NOT use for code generation.
example_trigger: "figure out the optimal cron schedule", "evaluate whether approach A or B is safer"

### coding
model: qwen3-coder:30b
use_when: >
  You need to write, debug, refactor, or review code in any language. Also use for
  writing bash scripts longer than ~10 lines.
example_trigger: "write a Python scraper", "fix the bug in this function", "write a systemd unit"

### fast
model: qwen3:4b-instruct
use_when: >
  The task is simple, low-stakes, and speed matters more than depth. Examples: summarising
  a short file, answering a factual question, reformatting text.
example_trigger: "summarise this log file", "what does this flag do"

### bootstrap
model: phi4-mini:latest
use_when: >
  Used only during first-run wizard. Not available in the main agent loop.
```

---

### `workspace/user_profile.md`

Persistent user profile. Populated during the wakeup wizard; updated by the user or agent
over time. Injected into every system prompt as context.

```markdown
# User Profile

name: Alex
timezone: Europe/Zagreb
preferred_language: English
working_style: direct, no unnecessary explanations
current_projects:
  - Ora OS development
  - Stock price scraper in Python
notes: >
  Prefers concise responses. Comfortable with Linux. Wants the agent to act autonomously
  and ask for confirmation only when an action is destructive or irreversible.
```

---

### `workspace/hardware_profile.md`

Auto-generated on every boot by `hardware_probe.py`. Never manually edited during a run.
Includes GPU backend detection for NVIDIA (CUDA), AMD (ROCm), and Apple Silicon (Metal).

```markdown
# Hardware Profile
_Generated: 2025-01-01 10:00:00_

## CPU
model: AMD Ryzen 9 7950X
cores: 32
ram_total_gb: 64.0
ram_available_gb: 48.2

## GPU 0
vendor: NVIDIA
model: RTX 4090
vram_total_gb: 24.0
vram_available_gb: 21.5
backend: cuda

## GPU 1
not present

## Ollama GPU backend
detected: cuda
fallback: cpu

## Model fit summary
| model                 | size_gb | fits_vram | fits_ram |
|-----------------------|---------|-----------|----------|
| phi4-mini:latest      | 2.5     | ✅        | ✅       |
| qwen3-coder:30b       | 18.5    | ✅        | ✅       |
| deepseek-r1:14b       | 9.0     | ✅        | ✅       |
| qwen3:4b-instruct     | 2.5     | ✅        | ✅       |
| devstral:latest       | 14.0    | ✅        | ✅       |

## Parallel load feasibility
qwen3-coder:30b (18.5) + qwen3:4b-instruct (2.5) = 21.0 GB → ✅ fits together
qwen3-coder:30b (18.5) + deepseek-r1:14b (9.0) = 27.5 GB → ❌ too large
```

---

### `workspace/session_state.md`

Overwritten after every turn. Live view of what the agent is doing.

```markdown
# Session State
_Last updated: 2025-01-01 10:04:22_

## Active model
model: qwen3-coder:30b
context_window: 32768
tokens_used: 18432
tokens_used_pct: 56.3%
overflow_threshold_pct: 82.0%

## Switch log
| time     | from              | to                  | reason                              |
|----------|-------------------|---------------------|-------------------------------------|
| 10:02:11 | qwen3-coder:30b   | deepseek-r1:14b     | reasoning role requested            |
| 10:02:58 | deepseek-r1:14b   | qwen3-coder:30b     | reasoning complete, returned result |
```

---

### `workspace/memory/context_summary.md`

Compressed rolling summary of the current session. Written by `context_manager.py` each
time an overflow is triggered. Kept under `max_summary_tokens` (default 400 tokens) so it
does not itself become a context burden. Injected at the start of every new session.

```markdown
# Context Summary
_Last summarised: 2025-01-01 10:08:44_
_Overflow events this session: 2_

## Summary
User is setting up Ora OS on a fresh Ubuntu 24.04 machine. Completed: installed Ollama,
pulled qwen3-coder:30b and deepseek-r1:14b, confirmed CUDA backend active.
In progress: writing the systemd unit file for auto-start on boot.
Blocked: systemd service fails with exit code 203 — likely a PATH issue with the venv.

## Open tasks
- Fix systemd PATH issue in ora_os.service
- Test remote access via Tailscale SSH

## Last 4 raw turns
[appended verbatim by context_manager.py]
```

---

### `workspace/memory/persistent_memory.md`

Long-term facts that survive across sessions. The agent can append entries; the user can
edit or delete them freely. This is the v1 foundation for a future proper memory layer.

```markdown
# Persistent Memory

## Facts
- Machine hostname: ora-box
- Ollama installed at: /usr/local/bin/ollama
- Default Python venv: /opt/ora_os/.venv
- User prefers qwen3-coder:30b for most tasks

## Completed milestones
- 2025-01-01: First boot wizard completed
- 2025-01-02: Systemd auto-start configured

## Notes from agent
- deepseek-r1:14b tends to be verbose; prompt it to be concise
```

---

## Tools

### `hardware_probe.py`

Called automatically on every boot (not a model-callable tool).

- Reads RAM via `psutil`
- Detects NVIDIA GPUs via `pynvml`; AMD via `subprocess rocm-smi`; Apple Silicon via
  `platform` + `subprocess system_profiler`; falls back to CPU-only if none detected
- Queries `GET /api/tags` on Ollama for all pulled models and their sizes
- Scores each model in `viable_models.md` against current free VRAM and RAM
- Computes parallel load feasibility for all viable model pairs
- Writes `workspace/hardware_profile.md`
- Returns a compact hardware summary string injected into the system prompt

---

### `ollama_manager.py` — tools: `list_models()`, `pull_model(model_name)`

**`list_models()`** — returns the current contents of `viable_models.md` plus live fit
scores from the latest hardware profile. The agent calls this when it needs to reason about
which model to switch to.

**`pull_model(model_name)`** — pulls a model from Ollama's registry if:
- The model is listed in `viable_models.md` with `auto_pull: yes`
- The model fits within available VRAM or RAM
- User confirmation is obtained (always required for pulls — this involves a download)

Prints download progress to terminal. On completion, updates `hardware_profile.md` fit scores.

---

### `model_switcher.py` — tool: `switch_model(role, task_prompt, transfer_context)`

Called by the LLM as a tool call when it determines a specialist model is needed.

```python
def switch_model(
    role: str,              # e.g. "reasoning", "coding", "fast" — maps via model_roles.md
    task_prompt: str,       # the specific sub-task to hand off
    transfer_context: str,  # compact summary of relevant prior context (≤ 500 tokens)
) -> str:                   # specialist model's response, returned as tool result
```

**Behaviour:**
1. Resolves `role` → `target_model` via `workspace/model_roles.md`.
2. Validates model is in `viable_models.md` and fits in VRAM/RAM — refuses with explanation
   if not.
3. If `require_user_confirm_switch: true`, pauses and prints confirmation prompt.
4. Builds a minimal fresh message list for the specialist:
   ```
   system: "You are a specialist assistant. Context: {transfer_context}"
   user:   {task_prompt}
   ```
5. Calls the specialist via Ollama `/v1/chat/completions`.
6. Returns the specialist's response as the tool result — flows into the primary model's
   conversation history like any other tool result.
7. Updates `workspace/session_state.md` switch log.
8. The specialist model unloads after the call (Ollama TTL set to `0s` for switched models).

The calling model is responsible for writing a concise `transfer_context` — only what the
specialist needs, not the full history. The system prompt instructs it to keep this under
500 tokens.

---

### `context_manager.py`

Called automatically after every LLM response — invisible to the model.

**Trigger:** `tokens_used / context_window >= overflow_threshold` (default 0.82)

**On trigger:**
1. Takes all messages except the last `summary_keep_last_n_turns` turns.
2. Sends them to the current active model with a summarisation prompt.
3. Compresses the result to `max_summary_tokens` (default 400).
4. Appends summary to `workspace/memory/context_summary.md`.
5. Rebuilds messages as: `[system_prompt, summary_message, ...last_n_turns]`.
6. Logs the event to `workspace/session_state.md`.
7. Prints to terminal: `[context] 82% full — summarised and compacted. Continuing...`

The user sees no interruption. The session continues seamlessly.

---

### `bash_tool.py` — tool: `run_bash(command)`

Extended from the original `bash_agent_assistant` to cover the full Linux OS scope for v1.
All commands are Linux-native; no Windows/macOS compatibility is required.

**Permitted command categories (allowlist-based):**

| Category | Examples |
|---|---|
| File system | `ls`, `find`, `cat`, `cp`, `mv`, `mkdir`, `touch`, `chmod`, `chown`, `stat`, `du`, `df` |
| Process management | `ps`, `top`, `kill`, `systemctl status/start/stop/enable`, `crontab -l`, `htop` |
| Package management | `apt list`, `apt install`, `apt remove`, `pip install`, `npm install` |
| Networking | `curl`, `wget`, `ssh`, `ping`, `nmap`, `ip addr`, `netstat`, `ss` |
| General | `echo`, `grep`, `awk`, `sed`, `sort`, `wc`, `tar`, `unzip`, `which`, `env` |

**Hard-blocked regardless of allowlist** (configured in `config.md` → `bash_exclude_commands`):
- `rm -rf /` and variants targeting root or home
- `mkfs`, `dd if=/dev/zero`
- `shutdown`, `reboot` (unless explicitly unlocked in config)
- Any command writing directly to `/etc/passwd`, `/etc/shadow`, `/boot`

**Every command requires manual user confirmation** before execution — non-negotiable in v1.
The confirmation prompt shows the exact command string.

**Destructive operations** (`rm`, `mv` overwriting, `apt remove`) get an additional
`[DESTRUCTIVE]` warning tag in the confirmation prompt.

---

## System Prompt Template

Injected at the start of every session and after every context compaction:

```
You are Ora OS — an autonomous local AI agent running on Linux via Ollama.

[user]
{user_profile}

[previous session summary]
{context_summary}

[hardware]
{hardware_summary}

[available models and roles]
{model_roles}

[persistent memory]
{persistent_memory}

[tools]
- run_bash(command): execute a Linux shell command (requires user confirmation)
- switch_model(role, task_prompt, transfer_context): delegate a sub-task to a specialist
  model. role must be one of: reasoning | coding | fast. Write transfer_context in ≤500
  tokens — only what the specialist needs to know. Their response returns as a tool result.
- list_models(): show viable_models.md with live hardware fit scores
- pull_model(model_name): pull a new model from Ollama (requires user confirmation)

[rules]
- Always use run_bash for shell commands — never assume a command ran without calling it.
- Prefer VRAM-fit models when switching. Only use RAM-only models if no VRAM model fits.
- Do not switch models for trivial tasks. Switch only when the role description matches.
- Keep transfer_context concise (≤500 tokens). Do not dump the full conversation history.
- You may append facts to workspace/memory/persistent_memory.md when you learn something
  worth remembering across sessions.
- You are running on a dedicated Linux machine. You may interact broadly with the OS but
  must always confirm destructive or irreversible actions with the user.
```

---

## Agent Loop (`main.py`)

```
boot
  ├── print safety warning (full on first run, brief on subsequent)
  ├── load config.md
  ├── hardware_probe() → hardware_profile.md
  ├── load viable_models.md + model_roles.md
  ├── load user_profile.md + persistent_memory.md + context_summary.md
  ├── select active model (config default or interactive prompt)
  └── build system_prompt

per-turn loop
  ├── call LLM (active model)
  ├── context_manager.check()               ← transparent overflow check after every response
  │     if triggered → summarise → rebuild messages → continue
  ├── if tool_call == "run_bash":
  │     print command + [DESTRUCTIVE] tag if applicable
  │     confirm with user → execute on Linux → append result
  ├── if tool_call == "switch_model":
  │     resolve role → validate hardware fit → optional user confirm
  │     call specialist via Ollama → append result → update session_state.md
  ├── if tool_call == "list_models":
  │     return viable_models.md + live hardware fit scores
  ├── if tool_call == "pull_model":
  │     validate (viable list + hardware fit) → confirm with user → pull → update profiles
  └── if no tool_call:
        print response → write session_state.md → next user input

on exit (Ctrl+C or "exit")
  ├── write final context_summary.md (compact, ≤ max_summary_tokens)
  └── write session_state.md
```

---

## Dependencies

```
# requirements.txt
langgraph>=1.0.1
langchain-openai>=1.0.1
openai>=2.6.0
psutil>=5.9.0
pynvml>=11.5.0        # NVIDIA VRAM detection — gracefully absent if not installed
tiktoken>=0.7.0       # token counting for context overflow detection
rich>=13.0.0          # terminal formatting (safety warnings, tables, confirmation prompts)
```

---

## v1 Scope Boundaries

| In scope | Out of scope (future versions) |
|---|---|
| Terminal interface (shell / Tailscale SSH) | Web UI, REST API, mobile app |
| Single machine | Multi-machine / cluster |
| Sequential model switching | Parallel multi-model execution |
| Summary-based memory | Vector / RAG memory |
| Ollama local models only | Cloud API fallback |
| Linux (Ubuntu/Debian focus) | Windows, macOS |
| Manual `viable_models.md` + `model_roles.md` | Automated model discovery and benchmarking |

---

## Success Criteria

| Behaviour | Verification |
|---|---|
| Safety warning displayed and acknowledged before any action | Visible on every boot; blocks without `I UNDERSTAND` |
| Wakeup wizard populates all workspace markdown files | All files exist and are valid after first run |
| Bootstrap model self-configures from `config.md` | Change `bootstrap_model`, rerun — uses new model |
| Hardware profile accurate for NVIDIA, AMD, CPU fallback | Matches `nvidia-smi` / `rocm-smi` / `psutil` output |
| Viable models list respects VRAM/RAM limits | Agent refuses to load a model that does not fit |
| Agent pulls missing viable models with confirmation | `pull_model` works end-to-end with user confirm |
| Model switching resolves role → model via `model_roles.md` | Correct model called per role |
| Context transfer stays ≤ 500 tokens | Measurable in session_state.md switch log |
| Overflow triggers at 82%, session continues uninterrupted | `context_summary.md` updated, no crash or data loss |
| Session summary on exit is ≤ max_summary_tokens | Injected cleanly into next session system prompt |
| Persistent memory survives session close/reopen | `persistent_memory.md` persists across runs |
| All bash commands require confirmation | No command executes without user `y` |
| All state is human-readable and editable markdown | All workspace files open correctly in any text editor |
