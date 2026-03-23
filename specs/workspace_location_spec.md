# O.R.A. — Workspace Location Specification v0.1

### O.R.A. — Orchestrated Reasoning Agent

> Companion spec to `project_spec.md` and `settings_spec.md`. Defines where the
> workspace directory lives on disk, how the user chooses the location, and the
> safety mechanisms that prevent private data from leaking into version control.

---

## Problem

The workspace directory contains files with private or machine-specific data:

| File | Sensitive content |
|---|---|
| `user_profile.md` | Real name, timezone, working style |
| `config.md` | Ollama URLs, safety preferences |
| `network_config.md` | IP addresses of remote Ollama nodes |
| `network_trust.md` | Per-model trust decisions |
| `memory/persistent_memory.md` | Long-term personal facts |
| `memory/context_summary.md` | Conversation history summaries |

If the workspace lives inside the project repository, a careless `git add .` or
an agent-initiated file edit followed by a commit can push all of this to GitHub.
The `.gitignore` entry for `workspace/` is a first line of defence but is easy to
bypass (`git add -f`) or miss when cloning to a new machine.

**Goal:** move the workspace to an OS-standard user data directory by default,
allow the user to choose a custom location during boot, and enforce an explicit
safety check whenever the chosen path falls inside a git repository.

---

## Solution: `platformdirs`

[`platformdirs`](https://pypi.org/project/platformdirs/) is a lightweight,
well-maintained Python library that returns the correct OS-specific directory for
user data, configuration, and caches.

Default workspace locations:

| OS | Default path |
|---|---|
| Linux | `~/.local/share/ora-os/` (XDG_DATA_HOME) |
| macOS | `~/Library/Application Support/ora-os/` |
| Windows | `%LOCALAPPDATA%\OraOS\ora-os\` |

These directories are outside any project repository by definition.

### Why not other approaches?

| Alternative | Problem |
|---|---|
| `QSettings` / Windows Registry | Designed for simple key-value pairs. The workspace is rich markdown files — they belong on the filesystem. |
| `~/.ora-os/` dotfile | Works on Linux, wrong on Windows/macOS. Not standard. |
| `~/.config/ora-os/` | XDG-correct on Linux only. `platformdirs` handles all platforms. |
| Keep in-repo with `.gitignore` | Relies on a single ignore rule. Too easy to bypass or forget. |

---

## Architecture

### Workspace pointer: `workspace.conf`

A tiny file stored in the OS config directory that contains the absolute path to
the workspace. This solves the chicken-and-egg problem: you need to find the
workspace to read `config.md`, but `config.md` is inside the workspace.

| OS | `workspace.conf` location |
|---|---|
| Linux | `~/.config/ora-os/workspace.conf` |
| macOS | `~/Library/Application Support/ora-os/workspace.conf` |
| Windows | `%LOCALAPPDATA%\OraOS\ora-os\workspace.conf` |

Contents — a single line with the absolute path:

```
/home/alex/.local/share/ora-os
```

### Resolver priority

When O.R.A. starts, it resolves the workspace in this order:

```
1. Read workspace.conf → if the path exists and contains config.md, use it
2. Check legacy path: <script_dir>/workspace/ → backward compat for existing installs
3. Fall back to platformdirs default → fresh install
```

If none of the above contain a `config.md`, the first-run wizard is triggered.

### Directory structure (unchanged)

The workspace layout is identical regardless of where it lives:

```
<workspace_dir>/
├── config.md
├── user_profile.md
├── hardware_profile.md
├── models.md
├── vision_config.md
├── session_state.md
├── network_config.md
├── network_trust.md
├── network_registry.md
└── memory/
    ├── context_summary.md
    └── persistent_memory.md
```

---

## Boot Wizard: Workspace Location Step

During the first-run wizard (`boot.py`), a new step is inserted **before** the
Ollama connectivity check:

```
Step 1/5: Workspace location

  O.R.A. stores configuration, memory, and session data in a workspace directory.
  By default this is your OS user-data folder, which keeps private files out of
  any git repository.

  Default: ~/.local/share/ora-os

  Use this location? [Y/n]:
```

If the user accepts, the default is used. If they decline, they can type an
alternative path:

```
  Enter workspace path: /home/alex/my-ora-workspace
```

After the user confirms a path, the **git safety check** runs automatically.

---

## Git Safety Check

The safety check runs every time a workspace path is chosen or resolved. It is
**not optional** — it cannot be skipped or silenced.

### Algorithm

```
1. Walk up from workspace_dir looking for a .git/ directory.
2. If no .git/ found → SAFE. Proceed.
3. .git/ found → workspace is inside a git repo.
   a. Run `git check-ignore -q <workspace_dir>` against the repo.
   b. If exit code 0 → the path is gitignored → SAFE. Proceed.
   c. If not gitignored → UNSAFE. Enter mitigation flow.
```

### Mitigation flow (when workspace is inside an un-ignored git repo)

```
  WARNING: The workspace path
    /home/alex/projects/ora-os/workspace
  is inside a git repository at
    /home/alex/projects/ora-os
  and is NOT covered by .gitignore.

  Your private data (user profile, IP addresses, memory) could be
  accidentally committed and pushed to a remote.

  [1] Add to .gitignore automatically (recommended)
  [2] Choose a different path
  [3] Abort setup

  Choice [1]:
```

**Option 1 — auto-fix:**
- Appends the workspace path (relative to repo root) to the repo's `.gitignore`
- Creates a `.gitignore` inside the workspace itself containing `*` (belt-and-suspenders)
- Re-runs `git check-ignore` to verify the fix took effect
- Proceeds only if verification passes

**Option 2 — re-prompt:**
- Returns to the workspace path prompt. The user picks a new path.
- The safety check runs again on the new path.

**Option 3 — abort:**
- Exits the wizard. No files are written.

### Inner `.gitignore` (belt-and-suspenders)

Regardless of location, the wizard always creates a `.gitignore` inside the
workspace directory:

```gitignore
# This directory contains private O.R.A. user data.
# It should NEVER be committed to version control.
*
```

This means even if someone manually moves the workspace into a repo and forgets
to update the repo's `.gitignore`, git will still ignore the contents.

---

## Subsequent Runs

On every boot (not just first run), `main.py` resolves the workspace via the
resolver and runs a **silent safety check**:

- If the workspace is inside a git repo and not gitignored, print a one-line
  warning to the terminal:
  ```
  [ora/safety] Workspace is inside a git repo and NOT gitignored. Run /settings to fix.
  ```
- If the inner `.gitignore` is missing, recreate it silently.

This catches situations where the user moved files around between sessions.

---

## Removed: `workspace_dir` from `config.md`

Previously, `config.md` contained a `workspace_dir` field. This created a
circular dependency: you need the workspace to read config, but config tells you
the workspace.

The `workspace_dir` field is removed from the default config template. The
workspace location is now managed exclusively by `workspace.conf`. If an old
config contains `workspace_dir`, it is ignored — the resolver is the source of
truth.

---

## Module: `tools/workspace_resolver.py`

Exposes the following functions:

| Function | Purpose |
|---|---|
| `get_default_workspace()` | Returns the platformdirs default path |
| `resolve_workspace(script_dir)` | Full resolver: workspace.conf → legacy → default |
| `save_workspace_location(path)` | Writes workspace.conf |
| `find_git_root(path)` | Walks up looking for `.git/` |
| `is_gitignored(path, repo_root)` | Checks via `git check-ignore` |
| `add_to_gitignore(path, repo_root)` | Appends relative path to `.gitignore` |
| `check_workspace_git_safety(path, console)` | Full safety check with interactive mitigation |
| `ensure_inner_gitignore(path)` | Creates/repairs the inner `.gitignore` |

---

## v1 Scope Boundaries

| In scope | Out of scope (future versions) |
|---|---|
| `platformdirs` default workspace location | Cloud sync of workspace |
| User-selectable path during boot wizard | Runtime workspace relocation |
| Git safety check with auto-fix | Pre-commit hook integration |
| Inner `.gitignore` belt-and-suspenders | Encryption at rest |
| Silent safety re-check on every boot | Multi-workspace support |
| Backward compat with legacy `./workspace/` | Automatic migration wizard |
