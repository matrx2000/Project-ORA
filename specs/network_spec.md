# Ora OS — Network Model Discovery Specification v0.2

### O.R.A. — Orchestrated Reasoning Agent

> Companion spec to `project_spec.md`. Defines how Ora OS discovers, evaluates,
> and uses Ollama instances running on other machines on the network.

---

## Overview

Ora OS can connect to multiple remote Ollama instances defined by the user as a list of
IP addresses and model descriptions in `workspace/network_config.md`. On every startup,
Ora scans all listed addresses, inventories their models, compares them to local options,
and **always asks the user before using any remote model** — even previously approved ones.

Local models have priority by default. Remote models are only suggested when they are
clearly better for the required role. The user decides at session start which remote models
(if any) to allow for that session.

**Core principles:**
- Local models always have priority over remote ones, even if remote is larger.
- Remote models are only suggested when they are a better role match or significantly larger.
- Ora always asks at startup if better remote options were found — never switches silently.
- Remote models generate text only. All tool execution stays on the local machine.
- First-time remote model approval offers a "remember my choice" option.
- All network and trust settings are manageable via chat using `/settings` mode.

---

## New Files

```
workspace/
├── network_config.md       # User-defined remote Ollama nodes + model descriptions
├── network_registry.md     # Auto-written on startup: discovered nodes + scored models
└── network_trust.md        # Remembered trust choices (persists across sessions)
```

---

### `workspace/network_config.md`

User-editable. Read on every boot. The `remote_models` section lets you describe each
remote model so the local LLM understands what it is dealing with when routing tasks.

```markdown
# Ora OS — Network Config

## Remote Ollama Nodes

| label         | address               | notes                            |
|---------------|-----------------------|----------------------------------|
| workstation   | 192.168.1.42:11434    | RTX 4090, big models             |
| nas-box       | 192.168.1.55:11434    | CPU only, small/fast models      |
| cloud-vps     | 10.8.0.3:11434        | Tailscale peer, reasoning models |

## Remote Model Descriptions
## These descriptions are injected into Ora's system prompt so it understands
## what each remote model is good for — same format as local model_roles.md

| node          | model                | role        | description                                              |
|---------------|----------------------|-------------|----------------------------------------------------------|
| workstation   | deepseek-r1:70b      | reasoning   | Large reasoning model, best for complex logic and planning |
| workstation   | llama3.3:70b         | general     | Large general-purpose model, strong at long-form tasks   |
| workstation   | qwen3-coder:30b      | coding      | Same as local but remote — useful if local VRAM is busy  |
| nas-box       | phi4-mini:latest     | fast        | Lightweight, CPU-only, good for quick lookups            |

## Scan Settings
scan_timeout_seconds: 3
notify_on_discovery: true

## Model Selection Policy
local_priority: true               # local models always used unless user approves remote
remote_preference_threshold: 0.20  # remote must score 20% better to be suggested
better_model_criteria:
  - role_match                     # remote model matches the role better
  - model_size                     # larger parameter count preferred

## Security
require_approval_for_new_models: true
remember_approvals: true           # offer "remember my choice" on first approval
```

---

### `workspace/network_registry.md`

Auto-generated on every startup. Shows live status of all nodes and model scores.

```markdown
# Network Registry
_Scanned: 2025-01-01 10:00:05_

## Local models (priority)
| model                | size_gb | role       |
|----------------------|---------|------------|
| qwen3-coder:30b      | 18.5    | coding     |
| deepseek-r1:14b      | 9.0     | reasoning  |
| qwen3:4b-instruct    | 2.5     | fast       |

## Remote nodes

### workstation (192.168.1.42:11434) ✅ online — 12ms
| model                | size_gb | role       | vs local              | trust       |
|----------------------|---------|------------|-----------------------|-------------|
| deepseek-r1:70b      | 43.0    | reasoning  | ⬆️ 5x larger          | ✅ approved  |
| llama3.3:70b         | 43.0    | general    | no local equivalent   | ⚠️ pending  |
| qwen3-coder:30b      | 18.5    | coding     | same size as local    | ✅ approved  |

### nas-box (192.168.1.55:11434) ✅ online — 8ms
| model                | size_gb | role       | vs local              | trust       |
|----------------------|---------|------------|-----------------------|-------------|
| phi4-mini:latest     | 2.5     | fast       | same as local         | ✅ approved  |

### cloud-vps (10.8.0.3:11434) ❌ offline

## Startup recommendation
Better remote option found for role 'reasoning':
  deepseek-r1:70b on workstation (43.0 GB) vs local deepseek-r1:14b (9.0 GB) — 5x larger
  → Will ask user before using.
```

---

### `workspace/network_trust.md`

Records remembered trust choices. Written when the user selects "remember my choice".
User can edit directly or manage via `/settings` mode.

```markdown
# Network Trust Registry

## Remembered approvals
| node          | model                | decision  | remembered_at       |
|---------------|----------------------|-----------|---------------------|
| workstation   | deepseek-r1:70b      | approved  | 2025-01-01 10:01:22 |
| workstation   | qwen3-coder:30b      | approved  | 2025-01-01 10:01:25 |
| nas-box       | phi4-mini:latest     | approved  | 2025-01-01 10:01:28 |
| workstation   | llama3.3:70b         | declined  | 2025-01-01 10:01:30 |

## Permanently revoked
## Add entries here to block a model from ever being offered again
| node          | model                | reason                           |
|---------------|----------------------|----------------------------------|

## Notes
- Remembered approvals are still shown at startup — the user sees them and can change them.
- "Remembered" means the default answer is pre-filled, not that the prompt is skipped.
- Delete a row to require a fresh decision next session.
- Add to Revoked to suppress permanently.
```

---

## Startup Scan Flow

On every boot, after hardware probe and before entering the agent loop:

```
1. Read network_config.md — get list of nodes + remote model descriptions
2. For each node: GET /api/tags with scan_timeout_seconds
   → mark online/offline, record latency
3. For each online node: inventory available models
4. Cross-reference each model against network_trust.md:
   → known approved  → ✅ approved (remembered default = yes)
   → known declined  → remembered default = no, but still shown
   → never seen      → ⚠️ pending (no default, must decide)
   → in revoked list → silently excluded, never shown
5. Score remote models vs local using better_model_criteria
6. If any remote model scores > local by remote_preference_threshold:
   → add to "startup recommendation" in network_registry.md
7. Print startup summary and ask user
```

**Startup prompt example** (shown only if remote models found):

```
[ora] Network scan complete — 2 of 3 nodes online.

  Better remote option found:
  ┌─────────────────────────────────────────────────────────────┐
  │  Role: reasoning                                            │
  │  Local:  deepseek-r1:14b  (9.0 GB)  on this machine        │
  │  Remote: deepseek-r1:70b  (43.0 GB) on workstation         │
  │  Difference: 5x larger, same role                          │
  └─────────────────────────────────────────────────────────────┘

  Use remote deepseek-r1:70b for reasoning tasks this session? [y/N]:
  Remember this choice? [y/N]:

  Other approved remote models available (no local competition):
  - llama3.3:70b on workstation — remembered: declined (change? [y/N]):

  ⚠️  New model requires approval:
  - qwen3-coder:32b on workstation — first time seen
    Description: "Large coder, strong at refactoring"
    Approve for this session? [y/N]:
    Remember this choice? [y/N]:

[ora] Session starting with: local models + deepseek-r1:70b (remote, reasoning role)
```

If no remote models are found or all are offline, this prompt is skipped entirely.

---

## Updated `model_switcher.py`

Role resolution now checks remote candidates in addition to local ones.

**Resolution order:**

1. Check local `viable_models.md` for a model matching the role — this is always the
   default candidate.
2. Check `network_registry.md` for remote models matching the role that are:
   - Node: online
   - Trust: approved for this session
   - Score: exceeds local by `remote_preference_threshold`
3. If a better remote candidate exists **and the user approved it at startup**, use it.
4. If the user did not approve it at startup, use local — never ask again mid-task.
5. Call the selected model, return result as tool result.
6. Log to `session_state.md` with node address noted.

**Remote system prompt — hard block on tool execution:**

Every call to a remote model prepends this to the system prompt and it cannot be
overridden:

```
You are a specialist text-generation assistant for Ora OS.
Your output is plain text only.
You must not output tool calls, JSON function calls, bash commands, or any
instruction for the host system to execute. Any such output will be discarded.
```

Tool-call parsing is explicitly disabled for remote completions in code — even if
a remote model outputs a tool-call-shaped response, only the `.content` text field
is extracted. The tool dispatch loop never runs on remote outputs.

**Offline fallback:**

If a remote node becomes unreachable mid-call:
```
[ora] Remote node 'workstation' is not responding. Falling back to local deepseek-r1:14b.
```
Ora retries the task locally, logs the failure, and continues without interruption.

---

## Remote Model Descriptions in System Prompt

The descriptions from `network_config.md` → `Remote Model Descriptions` are injected into
Ora's system prompt alongside local `model_roles.md`, so the agent can reason about remote
options when deciding whether to call `switch_model()`:

```
[remote models — approved for this session]
- deepseek-r1:70b on workstation (reasoning):
  "Large reasoning model, best for complex logic and planning. 5x larger than local."
- Use switch_model(role="reasoning") to route to this model automatically.
```

---

## Security Summary

| Threat | Mitigation |
|---|---|
| Rogue model generating tool calls | Tool-call parsing disabled for all remote completions |
| Unknown model used without consent | Every new model requires `[y/N]` approval at startup |
| Approved model silently used every session | Remembered choices still shown at startup — user sees and can change them |
| Remote node goes offline mid-session | Per-call check + automatic local fallback |
| Bad model permanently approved | Edit `network_trust.md` directly or via `/settings` to revoke |
| Model weights swapped on remote node | Model name + node label are trust key — name change triggers re-approval |

---

## v1 Scope Boundaries

| In scope | Out of scope (future versions) |
|---|---|
| Static IP list in `network_config.md` | mDNS / Zeroconf LAN auto-discovery |
| Manual Tailscale for VPN peers | Built-in Tailscale integration |
| Text-only remote model calls | Remote tool execution |
| Per-session approval with remember option | Cryptographic node/model authentication |
| Single remote model per switch | Parallel calls to multiple remote models |
| Settings manageable via `/settings` chat mode | Full web UI for network management |

---

## Dependencies Added

```
# addition to requirements.txt
httpx>=0.27.0    # async HTTP for parallel node scanning with timeout control
```
