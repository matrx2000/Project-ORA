# O.R.A. — Settings Mode Specification v0.1

### O.R.A. — Orchestrated Reasoning Agent

> Companion spec to `project_spec.md` and `network_spec.md`. Defines how the user
> can configure O.R.A. through a natural language chat interface mid-session.

---

## Overview

Settings mode is a special conversational state within a running O.R.A. session. The user
enters it by typing `/settings` at any point. Ora switches into a focused configuration
assistant persona, helps the user read and modify any workspace markdown file, shows a diff
of proposed changes, and writes them only after explicit confirmation.

Settings mode is a **state within the normal session** — not a separate startup mode. The
agent loop pauses normal task execution while in settings mode and resumes when the user
types `/done` or `exit settings`.

---

## Entering and Exiting

| Command | Effect |
|---|---|
| `/settings` | Enter settings mode |
| `/settings network` | Enter settings mode focused on network config |
| `/settings models` | Enter settings mode focused on model roles and viable models |
| `/settings profile` | Enter settings mode focused on user profile |
| `/settings safety` | Enter settings mode focused on bash safety rules |
| `/settings memory` | Enter settings mode focused on session/memory settings |
| `/done` or `/exit` | Exit settings mode, resume normal session |

On entering, Ora prints:

```
[ora/settings] Settings mode active. I can help you configure any aspect of O.R.A.
  Type /done to return to normal mode.
  What would you like to change?
```

---

## What Can Be Configured

Every workspace markdown file is accessible and editable through settings mode. The user
can ask in plain language — Ora translates the intent into a file edit.

### Network Settings (`network_config.md`, `network_trust.md`)
- Add or remove remote Ollama nodes
- Update model descriptions for remote models
- Change scan timeout, preference threshold
- View current trust registry
- Approve or revoke specific remote models
- Clear all remembered choices

**Example exchanges:**
```
you: add a new node called gpu-server at 192.168.1.99 port 11434
ora: I'll add gpu-server (192.168.1.99:11434) to your network_config.md.
     Show diff? [y/N]:

you: revoke llama3.3:70b on workstation
ora: I'll add llama3.3:70b / workstation to the Revoked list in network_trust.md.
     It will no longer be offered in future sessions.
     Confirm? [y/N]:

you: show me all remembered approvals
ora: [prints network_trust.md Approved table]

you: clear all remembered choices
ora: This will delete all rows from network_trust.md Approved and Declined.
     Every remote model will require a fresh decision next startup.
     Confirm? [y/N]:
```

### Model Settings (`viable_models.md`, `model_roles.md`)
- Add or remove models from the viable list
- Change which model is assigned to a role
- Update role descriptions
- Change the default model
- Enable or disable auto-pull for a model

**Example exchanges:**
```
you: set my default reasoning model to deepseek-r1:32b
ora: I'll update model_roles.md: reasoning → deepseek-r1:32b.
     Current: deepseek-r1:14b
     New:     deepseek-r1:32b
     Confirm? [y/N]:

you: add mistral-small:22b to viable models, role fast, auto_pull yes
ora: I'll add this row to viable_models.md:
     | mistral-small:22b | 13.0 | fast | — | yes |
     Confirm? [y/N]:
```

### User Profile (`user_profile.md`)
- Update name, timezone, working style
- Add or remove current projects
- Edit notes/preferences

**Example exchanges:**
```
you: add "building a stock scraper" to my current projects
ora: I'll append "building a stock scraper" to current_projects in user_profile.md.
     Confirm? [y/N]:

you: change my working style to "very concise, bullet points preferred"
ora: I'll update working_style in user_profile.md.
     Confirm? [y/N]:
```

### Safety Settings (`config.md` → bash section)
- Add or remove blocked commands
- Toggle `bash_require_confirm`
- Change confirmation behaviour for destructive commands

**Example exchanges:**
```
you: block the wget command
ora: I'll add 'wget' to bash_exclude_commands in config.md.
     Note: wget is currently in the allow list. Blocking it will prevent
     any network downloads via bash.
     Confirm? [y/N]:

you: turn off confirmation for non-destructive commands
ora: I'll set bash_require_confirm: false in config.md.
     ⚠️  This means Ora will execute non-destructive bash commands without asking.
     Destructive commands (rm, mv overwrite) will still require confirmation.
     Confirm? [y/N]:
```

### Memory and Session Settings (`config.md` → context section)
- Change overflow threshold
- Change summary length
- Change number of raw turns preserved after summary
- Reset the current context summary

**Example exchanges:**
```
you: lower the overflow threshold to 75%
ora: I'll set overflow_threshold: 0.75 in config.md.
     This will trigger context summarisation earlier in long sessions.
     Confirm? [y/N]:

you: clear the context summary
ora: I'll delete the contents of workspace/memory/context_summary.md.
     The next session will start without a previous summary.
     Confirm? [y/N]:
```

---

## How Changes Are Applied

Ora always follows this flow for any settings change:

```
1. Parse the user's intent
2. Identify which file and field(s) need to change
3. Show a plain-language diff:
   ───────────────────────────────
   File: workspace/network_config.md
   Change: add row to Remote Ollama Nodes table
   + | gpu-server | 192.168.1.99:11434 | new node |
   ───────────────────────────────
4. Ask: "Confirm? [y/N]:"
5. If confirmed: write the file, print "[ora/settings] ✅ network_config.md updated."
6. If declined: print "[ora/settings] No changes made." and continue the conversation
7. Changes take effect immediately for the current session (no restart needed)
   except for: default_model change (takes effect next session)
```

Ora never writes to any file in settings mode without a confirmed `y`. Multi-file changes
(e.g. changing a model role that affects both `model_roles.md` and `viable_models.md`)
are shown as a combined diff and confirmed in one step.

---

## Settings Mode Persona

While in `/settings`, Ora adopts a focused, concise assistant persona — less conversational
than normal mode, more structured. It:

- Does not run bash commands
- Does not call switch_model
- Does not use any tools except `read_file` and `write_file` on workspace markdown files
- Responds in short, structured messages with clear confirm/cancel prompts
- Always shows what will change before changing it

The system prompt is temporarily replaced with a settings-specific one:

```
You are O.R.A. in settings mode. Your only job is to help the user read and
modify workspace configuration files. You may only read and write files in
the workspace/ directory. You must always show a plain-language diff and
receive explicit confirmation before writing any file. Never run bash commands
or call any model-switching tools. When the user types /done or /exit, settings
mode ends and normal operation resumes.
```

---

## Persistent Memory Edit via Settings

The user can also manage `workspace/memory/persistent_memory.md` through settings mode:

```
you: add a note that I prefer 4-space indentation in all Python code
ora: I'll append to persistent_memory.md:
     + - User prefers 4-space indentation in Python code
     Confirm? [y/N]:

you: show me everything in my persistent memory
ora: [prints persistent_memory.md]

you: clear all agent notes from persistent memory
ora: I'll delete all rows under "Notes from agent" in persistent_memory.md.
     Your Facts and Completed milestones sections will be preserved.
     Confirm? [y/N]:
```

---

## Settings Mode in the Agent Loop

```python
# pseudocode — agent loop addition

if user_input.startswith("/settings"):
    focus = parse_settings_focus(user_input)  # e.g. "network", "models", None
    session.mode = "settings"
    session.settings_focus = focus
    swap_system_prompt(SETTINGS_SYSTEM_PROMPT)
    print("[ora/settings] Settings mode active...")

elif user_input in ("/done", "/exit") and session.mode == "settings":
    session.mode = "normal"
    restore_system_prompt()
    print("[ora] Returning to normal mode.")

elif session.mode == "settings":
    # settings conversation — only file read/write tools available
    handle_settings_turn(user_input)

else:
    # normal agent loop
    handle_normal_turn(user_input)
```

---

## What Settings Mode Cannot Do

| Action | Why blocked |
|---|---|
| Run bash commands | Settings mode is config-only — no system execution |
| Pull new Ollama models | Use `pull_model()` in normal mode instead |
| Change files outside `workspace/` | Scope limited to configuration files |
| Modify `hardware_profile.md` | Auto-generated, not user-configurable |
| Modify `network_registry.md` | Auto-generated, not user-configurable |
| Change the wakeup wizard output retroactively | Re-run boot wizard for that |

---

## v1 Scope Boundaries

| In scope | Out of scope (future versions) |
|---|---|
| All workspace markdown files editable via chat | Web UI settings panel |
| Diff + confirm before every write | Undo/redo for settings changes |
| `/settings [focus]` shortcut commands | Settings search / autocomplete |
| Immediate effect for most settings | Hot-reload of model roles mid-session |
| Persistent memory management | Structured memory editor |
