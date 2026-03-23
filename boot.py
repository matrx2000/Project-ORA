"""
boot.py — First-run wakeup wizard for O.R.A.
Runs only when workspace/config.md does not exist.
Scans local Ollama models and lets the user assign roles with smart defaults.
"""
import datetime
import re
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from tools.hardware_probe import probe_hardware, query_ollama_models
from tools.workspace_resolver import (
    get_default_workspace, save_workspace_location, check_workspace_git_safety,
    ensure_inner_gitignore, WorkspaceRepick,
)

console = Console()

SAFETY_WARNING = """
O.R.A. has unrestricted access to the Linux filesystem and can install packages,
manage processes, and modify system state.

  Run it on a DEDICATED machine that does not contain personal data.
  Do not run it on your daily driver.

You have been warned.
"""

DEFAULT_CONFIG_CONTENT = """\
# O.R.A. Config

## Ollama
base_url: {base_url}

## Default model for main agent loop
## Leave blank to prompt at startup
default_model: {default_model}

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
bash_restrict_to_workspace: true
bash_warn_destructive: true

## Session
auto_save_session_state: true
auto_reload_config: false
"""

DEFAULT_USER_PROFILE = """\
# User Profile

name: {name}
timezone: UTC
preferred_language: English
working_style: direct, minimal explanations
current_projects:
  - O.R.A. setup
notes: >
  User completed the first-run wizard.
"""

DEFAULT_VISION_CONFIG = """\
# Vision Config

## Strategy
default_vision_strategy: describe_then_reason

## Description prompt
vision_description_prompt: >
  Describe this image in precise detail. If it contains text, code, terminal output,
  error messages, logs, or UI elements, transcribe them exactly.
  If it contains a diagram, chart, or visual data, describe the structure and values.
  Be thorough — your description will be used by another model to answer the user's question.

## File types treated as images
image_extensions: .png, .jpg, .jpeg, .gif, .webp, .bmp, .tiff

## File types passed as text (no vision model needed)
text_extensions: .txt, .md, .py, .js, .ts, .json, .yaml, .yml, .log, .csv, .sh

## File types not supported in v1
unsupported_extensions: .pdf, .docx, .xlsx, .zip

## Fallback when no vision model is configured
no_vision_model_response: >
  I received an image but no vision-capable model is configured.
  To enable image understanding, assign a vision-capable model to the 'vision' role
  in models.md via /settings.
"""


# ---------------------------------------------------------------------------
# Role suggestion engine
# ---------------------------------------------------------------------------

# (regex pattern, suggested_role, capabilities, description)
# Most specific patterns first — first match wins
_ROLE_HINTS = [
    (r"qwen2\.5-vl|qwen-vl|llava|moondream|bakllava|minicpm-v|llama.*vision",
     "vision", "text,images", "Multimodal vision model"),
    (r"deepseek-r1|qwq|reflection",
     "reasoning", "text", "Deep reasoning and analysis"),
    (r"qwen.*coder|codellama|starcoder|deepseek-coder|codegemma",
     "coding", "text", "Code generation and review"),
    (r"phi[234]|smollm|tinyllama|gemma:2b|qwen.*[01]\.5b",
     "fast", "text", "Fast lightweight model"),
    (r"ministral|qwen|llama|gemma|mistral|phi|yi|internlm|vicuna|command-r",
     "instruct", "text", "General instruct model"),
]


def _suggest_role(model_name: str) -> tuple[str, str, str]:
    """Return (role, capabilities, description) for a model name."""
    lower = model_name.lower()
    for pattern, role, caps, desc in _ROLE_HINTS:
        if re.search(pattern, lower):
            return role, caps, desc
    return "instruct", "text", "General purpose model"


def _suggest_bootstrap(pulled: dict[str, float]) -> str:
    """Suggest the best bootstrap model from pulled models."""
    # Prefer ministral-3b
    for name in pulled:
        if "ministral" in name.lower():
            return name
    # Otherwise smallest model
    if pulled:
        return min(pulled, key=lambda n: pulled[n])
    return "ministral-3b"


# ---------------------------------------------------------------------------
# models.md builder
# ---------------------------------------------------------------------------

_ROLE_USE_WHEN = {
    "instruct": (
        "You are handling a general task, answering questions, or executing tool calls. "
        "This is the primary model for the main agent loop."
    ),
    "reasoning": (
        "You need to reason through a logical problem, evaluate trade-offs, solve a mathematical "
        "challenge, or plan a multi-step approach before acting."
    ),
    "coding": (
        "You need to write, debug, refactor, or review code in any language. "
        "Also use for writing bash scripts longer than ~10 lines."
    ),
    "fast": (
        "The task is simple, low-stakes, and speed matters more than depth. "
        "Examples: summarising a short file, answering a factual question."
    ),
    "vision": (
        "The user has attached an image, screenshot, photo, or visual file. "
        "This model describes the visual content as detailed text, which is then "
        "passed to the instruct model for reasoning and action."
    ),
    "bootstrap": (
        "Used during first-run wizard and settings mode. "
        "Not used in the main agent loop."
    ),
}


def build_models_md(role_assignments: dict[str, dict]) -> str:
    """
    Build models.md content from role assignments.

    role_assignments: {role: {model, description, capabilities}}
    """
    lines = [
        "# O.R.A. Models",
        "",
        "# Single source of truth for model-to-role mapping.",
        "# Edit via /settings or the TUI settings popup.",
        "",
    ]

    for role, info in role_assignments.items():
        lines.append(f"### {role}")
        lines.append(f"model: {info['model']}")
        if info.get("description"):
            lines.append(f"description: {info['description']}")
        caps = info.get("capabilities", "text")
        if caps != "text":
            lines.append(f"capabilities: {caps}")
        use_when = _ROLE_USE_WHEN.get(role, f"Use for {role} tasks.")
        lines.append(f"use_when: >")
        lines.append(f"  {use_when}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------

def _require_understand() -> None:
    """Display safety warning and require 'I UNDERSTAND' to continue."""
    console.print(Panel(SAFETY_WARNING, title="[bold red]SAFETY WARNING[/bold red]", border_style="red"))
    while True:
        answer = Prompt.ask("[bold red]Type 'I UNDERSTAND' to proceed[/bold red]")
        if answer.strip() == "I UNDERSTAND":
            break
        console.print("[red]You must type exactly: I UNDERSTAND[/red]")


def _select_workspace_location(default_workspace: Path) -> Path:
    """Ask user where to store workspace. Runs git safety check."""
    platform_default = get_default_workspace()

    console.print("\n[bold]Step 1 — Workspace location[/bold]\n")
    console.print(
        "  O.R.A. stores config, memory, and session data in a workspace directory.\n"
        "  By default this is your OS user-data folder (keeps files out of git repos).\n"
    )
    console.print(f"  Default: [cyan]{platform_default}[/cyan]\n")

    while True:
        use_default = Confirm.ask("  Use this location?", default=True)
        workspace_dir = platform_default if use_default else Path(
            Prompt.ask("  Enter workspace path")
        ).expanduser().resolve()

        try:
            safe = check_workspace_git_safety(workspace_dir, console)
        except WorkspaceRepick:
            console.print("[dim]Pick a different path...[/dim]\n")
            continue

        if not safe:
            console.print("[red]Setup aborted by user.[/red]")
            sys.exit(1)

        workspace_dir.mkdir(parents=True, exist_ok=True)
        ensure_inner_gitignore(workspace_dir)
        save_workspace_location(workspace_dir)
        console.print(f"  [green]Workspace: {workspace_dir}[/green]\n")
        return workspace_dir


def _scan_ollama(ollama_base_url: str) -> tuple[bool, dict[str, float]]:
    """Check Ollama and return (reachable, {model: size_gb})."""
    console.print("\n[bold]Step 2 — Scanning Ollama[/bold]")

    try:
        import urllib.request
        with urllib.request.urlopen(ollama_base_url.rstrip("/") + "/api/tags", timeout=5) as resp:
            if resp.status == 200:
                pulled = query_ollama_models(ollama_base_url)
                if pulled:
                    console.print(
                        f"[green]Ollama is running.[/green] "
                        f"Found {len(pulled)} model(s):\n"
                    )
                    for name, size in pulled.items():
                        console.print(f"  [cyan]{name}[/cyan] ({size:.1f} GB)")
                    return True, pulled
                else:
                    console.print("[yellow]Ollama is running but no models are pulled.[/yellow]")
                    return True, {}
    except Exception:
        pass

    console.print(
        Panel(
            f"Cannot reach Ollama at [bold]{ollama_base_url}[/bold].\n\n"
            "  1. Install: [cyan]curl -fsSL https://ollama.com/install.sh | sh[/cyan]\n"
            "  2. Start:   [cyan]ollama serve[/cyan]\n"
            "  3. Pull:    [cyan]ollama pull ministral-3b[/cyan]",
            title="[bold yellow]Ollama not found[/bold yellow]",
            border_style="yellow",
        )
    )
    if not Confirm.ask("Continue without Ollama?", default=True):
        sys.exit(1)
    return False, {}


def _pick_bootstrap(pulled: dict[str, float], ollama_base_url: str) -> str:
    """Select bootstrap model from pulled models."""
    console.print("\n[bold]Step 3 — Bootstrap model[/bold]")
    console.print(
        "  This model runs the setup wizard. It must be already pulled.\n"
        "  [dim]Recommendation: ministral-3b (light and effective)[/dim]\n"
    )

    default = _suggest_bootstrap(pulled)

    if pulled:
        console.print("  Pulled models:")
        for name, size in pulled.items():
            marker = " [green]<-- suggested[/green]" if name == default else ""
            console.print(f"    [cyan]{name}[/cyan] ({size:.1f} GB){marker}")
        console.print()

    model = Prompt.ask("  Bootstrap model", default=default)

    if pulled and model not in pulled:
        console.print(f"[yellow]Warning: '{model}' is not pulled. Wizard may fail.[/yellow]")

    return model


def _assign_roles(pulled: dict[str, float], bootstrap_model: str) -> dict[str, dict]:
    """
    Let user assign roles to pulled models. Returns role_assignments dict.
    """
    console.print("\n[bold]Step 4 — Assign roles to your models[/bold]")
    console.print(
        "  For each model, accept the suggested role or type a new one.\n"
        "  Available roles: [cyan]instruct, reasoning, coding, fast, vision[/cyan]\n"
        "  Press Enter to accept the default. Type [cyan]skip[/cyan] to skip a model.\n"
    )

    role_assignments: dict[str, dict] = {}
    used_roles: set[str] = set()

    # Always add bootstrap
    role_assignments["bootstrap"] = {
        "model": bootstrap_model,
        "description": "Setup wizard and settings mode",
        "capabilities": "text",
    }

    for name, size in pulled.items():
        suggested_role, suggested_caps, suggested_desc = _suggest_role(name)

        # If the suggested role is already assigned, try a different one
        if suggested_role in used_roles:
            # Find an unassigned role
            for alt_role in ["instruct", "reasoning", "coding", "fast", "vision"]:
                if alt_role not in used_roles:
                    suggested_role = alt_role
                    suggested_desc = f"Assigned to {alt_role}"
                    break

        console.print(f"  [bold]{name}[/bold] ({size:.1f} GB)")
        console.print(
            f"    Suggested: [cyan]{suggested_role}[/cyan] — {suggested_desc}"
        )

        role_input = Prompt.ask(
            f"    Role",
            default=suggested_role,
        ).strip().lower()

        if role_input == "skip":
            console.print(f"    [dim]Skipped[/dim]\n")
            continue

        desc_input = Prompt.ask(
            f"    Description",
            default=suggested_desc,
        ).strip()

        # Auto-detect vision capabilities
        caps = suggested_caps if role_input == "vision" or "vl" in name.lower() else "text"

        role_assignments[role_input] = {
            "model": name,
            "description": desc_input,
            "capabilities": caps,
        }
        used_roles.add(role_input)
        console.print(f"    [green]{name} → {role_input}[/green]\n")

    # Ensure we have at least an instruct role
    if "instruct" not in role_assignments:
        if pulled:
            first_model = next(iter(pulled))
            console.print(
                f"[yellow]No instruct model assigned. Using {first_model} as instruct.[/yellow]"
            )
            role_assignments["instruct"] = {
                "model": first_model,
                "description": "Default instruct model",
                "capabilities": "text",
            }

    # Show summary
    console.print("\n[bold]Role assignments:[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Role")
    table.add_column("Model")
    table.add_column("Description")
    for role, info in role_assignments.items():
        table.add_row(role, info["model"], info.get("description", ""))
    console.print(table)

    if not Confirm.ask("\nLooks good?", default=True):
        console.print("[dim]Re-running role assignment...[/dim]")
        return _assign_roles(pulled, bootstrap_model)

    return role_assignments


def _wizard_chat(
    client: OpenAI, model: str, messages: list[dict], system_prompt: str,
) -> tuple[str, list[dict]]:
    """Send messages to the bootstrap model and return (reply, updated_messages)."""
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": system_prompt}] + messages
    try:
        response = client.chat.completions.create(model=model, messages=messages)
        reply = response.choices[0].message.content or ""
        messages = messages + [{"role": "assistant", "content": reply}]
        return reply, messages
    except Exception as exc:
        return f"[model error: {exc}]", messages


def _user_profile_session(
    bootstrap_model: str, ollama_base_url: str,
    pulled: dict[str, float], hardware_summary: str,
) -> str:
    """Run interactive user profile setup. Returns user name."""
    client = OpenAI(
        base_url=ollama_base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    model_list = "\n".join(f"  - {n} ({s:.1f} GB)" for n, s in pulled.items()) or "  (none)"

    system_prompt = (
        "You are helping configure O.R.A. (Orchestrated Reasoning Agent). "
        "Help the user set up their profile (name, timezone, working style, projects). "
        "Be concise. Ask one thing at a time."
        f"\n\nHardware: {hardware_summary}"
        f"\n\nPulled models:\n{model_list}"
    )

    messages: list[dict] = []

    console.print("\n[bold]Step 5 — User profile[/bold]")
    console.print("[dim]The bootstrap model will help set up your profile. Type 'done' when finished.[/dim]\n")

    opening = "Hello! I'll help set up your user profile. What's your name?"
    console.print(f"[bold cyan]Ora[/bold cyan]: {opening}")
    messages.append({"role": "assistant", "content": opening})

    user_name = "User"

    while True:
        try:
            user_input = Prompt.ask("[bold green]You[/bold green]")
        except (KeyboardInterrupt, EOFError):
            break

        if user_input.strip().lower() in ("done", "exit", "quit", "finish"):
            break

        messages.append({"role": "user", "content": user_input})
        reply, messages = _wizard_chat(client, bootstrap_model, messages, system_prompt)

        # Heuristic: extract name from early user messages
        if user_name == "User":
            for msg in messages[:6]:
                if msg.get("role") == "user":
                    words = msg["content"].split()
                    if len(words) <= 4 and words:
                        candidate = words[0].rstrip(",.!?").capitalize()
                        if candidate.isalpha() and len(candidate) >= 2:
                            user_name = candidate

        console.print(f"\n[bold cyan]Ora[/bold cyan]: {reply}\n")

    return user_name


# ---------------------------------------------------------------------------
# Workspace file writers
# ---------------------------------------------------------------------------

def _write_workspace_files(
    workspace_dir: Path,
    default_model: str,
    ollama_base_url: str,
    user_name: str,
    role_assignments: dict[str, dict],
) -> None:
    """Write all initial workspace files."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "memory").mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bootstrap_model = role_assignments.get("bootstrap", {}).get("model", "")

    # config.md
    (workspace_dir / "config.md").write_text(
        DEFAULT_CONFIG_CONTENT.format(
            base_url=ollama_base_url,
            default_model=default_model,
        ),
        encoding="utf-8",
    )

    # models.md (single source of truth for all model config)
    (workspace_dir / "models.md").write_text(
        build_models_md(role_assignments),
        encoding="utf-8",
    )

    # user_profile.md
    (workspace_dir / "user_profile.md").write_text(
        DEFAULT_USER_PROFILE.format(name=user_name),
        encoding="utf-8",
    )

    # vision_config.md
    (workspace_dir / "vision_config.md").write_text(
        DEFAULT_VISION_CONFIG, encoding="utf-8",
    )

    # session_state.md
    (workspace_dir / "session_state.md").write_text(
        f"# Session State\n_Last updated: {now}_\n\n"
        f"## Active model\nmodel: {default_model}\n"
        "tokens_used: 0\n\n"
        "## Switch log\n"
        "| time     | from              | to                  | reason                              |\n"
        "|----------|-------------------|---------------------|-------------------------------------|\n\n",
        encoding="utf-8",
    )

    # memory/context_summary.md
    (workspace_dir / "memory" / "context_summary.md").write_text(
        "# Context Summary\n_No sessions yet._\n\n## Summary\n(none)\n",
        encoding="utf-8",
    )

    # memory/persistent_memory.md
    (workspace_dir / "memory" / "persistent_memory.md").write_text(
        f"# Persistent Memory\n\n## Facts\n"
        f"- Setup completed: {now}\n"
        f"- Bootstrap model: {bootstrap_model}\n"
        f"- Default model: {default_model}\n\n"
        "## Notes from agent\n(none yet)\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

def run_wizard(workspace_dir: Path, ollama_base_url: str = "http://127.0.0.1:11434") -> Path:
    """
    Execute the first-run wakeup wizard.
    Returns the final workspace directory.
    """
    # Step 0: Safety
    _require_understand()

    # Step 1: Workspace
    workspace_dir = _select_workspace_location(workspace_dir)

    # Step 2: Ollama scan
    ollama_ok, pulled = _scan_ollama(ollama_base_url)

    # Step 3: Bootstrap model (first priority — needed for the rest of setup)
    bootstrap_model = _pick_bootstrap(pulled, ollama_base_url)

    # Step 4: Role assignment (scan-based, no tiers)
    role_assignments = _assign_roles(pulled, bootstrap_model)

    # Pick default model (instruct role)
    default_model = role_assignments.get("instruct", {}).get("model", "")
    if not default_model and pulled:
        default_model = next(iter(pulled))

    # Hardware probe
    console.print("\n[dim]Probing hardware...[/dim]")
    hardware_summary, _ = probe_hardware(workspace_dir, ollama_base_url)
    console.print(f"[dim]{hardware_summary}[/dim]")

    # Step 5: User profile (via bootstrap model)
    if ollama_ok:
        user_name = _user_profile_session(
            bootstrap_model, ollama_base_url, pulled, hardware_summary,
        )
    else:
        user_name = Prompt.ask("\n[bold]Your name[/bold]", default="User")

    # Write workspace files
    _write_workspace_files(
        workspace_dir, default_model, ollama_base_url, user_name, role_assignments,
    )

    console.print(
        Panel(
            f"Config saved to [bold]{workspace_dir}[/bold]\n"
            f"Default model: [cyan]{default_model or '(prompt at startup)'}[/cyan]\n"
            f"Roles configured: {len(role_assignments)}\n"
            "Launching O.R.A...",
            title="[bold green]Setup Complete[/bold green]",
            border_style="green",
        )
    )

    return workspace_dir
