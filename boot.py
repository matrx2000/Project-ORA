"""
boot.py — First-run wakeup wizard for O.R.A.
Runs only when workspace/config.md does not exist.
Includes hardware tier presets and custom model configuration.
"""
import datetime
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from tools.hardware_probe import probe_hardware
from tools.ollama_manager import (
    query_ollama_models, write_initial_viable_models, write_viable_models,
)
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

## Bootstrap model (used during first-run wizard)
bootstrap_model: {bootstrap_model}

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
# describe_then_reason (default): vision model describes the image as text,
#   then the instruct model reasons about it. Best for complex tasks.
# vision_handles_all: vision model generates the full response directly.
#   Use only for simple "what is in this image?" queries.
default_vision_strategy: describe_then_reason

## Description prompt
# Sent to the vision model when an image is received.
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

## Fallback behaviour when no vision model is configured
no_vision_model_response: >
  I received an image but no vision-capable model is configured.
  To enable image understanding, add a model with capabilities: text,images
  to workspace/viable_models.md and assign it the 'vision' role in model_roles.md.
"""


# ---------------------------------------------------------------------------
# Hardware tier presets
# ---------------------------------------------------------------------------

TIER_PRESETS = {
    "1": {
        "name": "Jetson Orin Nano / Low-end (<=8GB)",
        "description": "~5.5GB usable. One model at a time, hot-swap between calls.",
        "models": [
            {"model": "qwen3:4b", "size_gb": 2.5, "role": "instruct",
             "capabilities": "text", "notes": "default model, strong tool use", "auto_pull": True},
            {"model": "qwen2.5-vl:3b", "size_gb": 2.0, "role": "vision",
             "capabilities": "text,images", "notes": "vision model, Jetson-optimised", "auto_pull": True},
            {"model": "deepseek-r1:1.5b", "size_gb": 1.0, "role": "reasoning",
             "capabilities": "text", "notes": "light reasoning", "auto_pull": True},
            {"model": "phi4-mini:3.8b", "size_gb": 2.3, "role": "fast",
             "capabilities": "text", "notes": "fast alternative instruct", "auto_pull": True},
        ],
        "default_model": "qwen3:4b",
    },
    "2": {
        "name": "Mid-range desktop (RTX 3080 / ~10GB VRAM)",
        "description": "Can fit ~8B models. Hot-swap for larger pairs.",
        "models": [
            {"model": "qwen3:8b", "size_gb": 5.0, "role": "instruct",
             "capabilities": "text", "notes": "main instruct model", "auto_pull": True},
            {"model": "qwen2.5-vl:7b", "size_gb": 5.0, "role": "vision",
             "capabilities": "text,images", "notes": "vision model", "auto_pull": True},
            {"model": "deepseek-r1:7b", "size_gb": 4.5, "role": "reasoning",
             "capabilities": "text", "notes": "mid-range reasoning", "auto_pull": True},
            {"model": "qwen3:4b", "size_gb": 2.5, "role": "fast",
             "capabilities": "text", "notes": "fast tasks", "auto_pull": True},
        ],
        "default_model": "qwen3:8b",
    },
    "3": {
        "name": "High-end desktop (RTX 4090 / ~24GB VRAM)",
        "description": "Can co-load large instruct + vision. Fast pipeline.",
        "models": [
            {"model": "qwen3-coder:30b", "size_gb": 18.5, "role": "instruct",
             "capabilities": "text", "notes": "large coder/instruct", "auto_pull": True},
            {"model": "qwen2.5-vl:7b", "size_gb": 5.0, "role": "vision",
             "capabilities": "text,images", "notes": "vision model, co-loads with instruct", "auto_pull": True},
            {"model": "deepseek-r1:14b", "size_gb": 9.0, "role": "reasoning",
             "capabilities": "text", "notes": "strong reasoning", "auto_pull": True},
            {"model": "qwen3:4b", "size_gb": 2.5, "role": "fast",
             "capabilities": "text", "notes": "fast tasks", "auto_pull": True},
        ],
        "default_model": "qwen3-coder:30b",
    },
}


def _build_model_roles(models: list[dict], bootstrap_model: str) -> str:
    """Build model_roles.md content from a model list."""
    role_map: dict[str, dict] = {}
    for m in models:
        role = m["role"]
        if role not in role_map:
            role_map[role] = m

    lines = [
        "# Model Roles\n",
        "## Instructions to agent",
        "When you determine that a sub-task requires a specialist capability, consult this file",
        "to choose the correct model. Always prefer models marked as fits_vram: true.\n",
        "## Role definitions\n",
    ]

    role_descriptions = {
        "instruct": {
            "use_when": "You are handling a general task, answering questions, or executing tool calls. "
                        "This is the primary model for the main agent loop.",
            "example_trigger": '"answer a question", "execute a plan", "use tools"',
        },
        "reasoning": {
            "use_when": "You need to reason through a logical problem, evaluate trade-offs, solve a mathematical "
                        "challenge, or plan a multi-step approach before acting. Do NOT use for code generation.",
            "example_trigger": '"figure out the optimal cron schedule", "evaluate whether approach A or B is safer"',
        },
        "coding": {
            "use_when": "You need to write, debug, refactor, or review code in any language. Also use for "
                        "writing bash scripts longer than ~10 lines.",
            "example_trigger": '"write a Python scraper", "fix the bug in this function", "write a systemd unit"',
        },
        "fast": {
            "use_when": "The task is simple, low-stakes, and speed matters more than depth. Examples: summarising "
                        "a short file, answering a factual question, reformatting text.",
            "example_trigger": '"summarise this log file", "what does this flag do"',
        },
        "vision": {
            "use_when": "The user has attached an image, screenshot, photo, or visual file. "
                        "This model describes the visual content as detailed text, which is then "
                        "passed to the instruct model for reasoning and action. "
                        "Do NOT use for text-only tasks.",
            "example_trigger": '"user uploads screenshot", "user attaches photo"',
        },
    }

    for role, m in role_map.items():
        desc = role_descriptions.get(role, {
            "use_when": f"Use for {role} tasks.",
            "example_trigger": f'"{role} task"',
        })
        lines.append(f"### {role}")
        lines.append(f"model: {m['model']}")
        if m.get("capabilities", "text") != "text":
            lines.append(f"capabilities: {m['capabilities']}")
        lines.append(f"use_when: >")
        lines.append(f"  {desc['use_when']}")
        lines.append(f"example_trigger: {desc['example_trigger']}")
        if role == "vision":
            lines.append("vision_strategy: describe_then_reason")
        lines.append("")

    # Add bootstrap role
    lines.append("### bootstrap")
    lines.append(f"model: {bootstrap_model}")
    lines.append("use_when: >")
    lines.append("  Used only during first-run wizard. Not available in the main agent loop.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier selection UI
# ---------------------------------------------------------------------------

def _select_hardware_tier(pulled_models: dict[str, float]) -> tuple[list[dict], str]:
    """
    Present hardware tier presets or custom option.
    Returns (model_list, default_model_name).
    """
    console.print("\n[bold]Hardware Tier Presets[/bold]")
    console.print("Select a preset that matches your hardware, or choose Custom to configure manually.\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", width=3)
    table.add_column("Tier")
    table.add_column("Description")
    table.add_column("Models")

    for key, preset in TIER_PRESETS.items():
        model_names = ", ".join(m["model"] for m in preset["models"])
        table.add_row(key, preset["name"], preset["description"], model_names)
    table.add_row("4", "[bold cyan]Custom[/bold cyan]", "Add models manually one by one", "you decide")

    console.print(table)

    choice = Prompt.ask("\n[bold]Select tier[/bold]", choices=["1", "2", "3", "4"], default="1")

    if choice in TIER_PRESETS:
        preset = TIER_PRESETS[choice]
        console.print(f"\n[green]Selected:[/green] {preset['name']}")

        # Show what will be configured
        ptable = Table(title="Models", show_header=True, header_style="bold")
        ptable.add_column("Model")
        ptable.add_column("Size")
        ptable.add_column("Role")
        ptable.add_column("Capabilities")
        for m in preset["models"]:
            ptable.add_row(m["model"], f"{m['size_gb']}GB", m["role"], m.get("capabilities", "text"))
        console.print(ptable)

        if Confirm.ask("\nUse this configuration?", default=True):
            return preset["models"], preset["default_model"]
        else:
            # Fall through to custom
            console.print("[dim]Switching to custom configuration...[/dim]\n")

    # Custom configuration
    return _custom_model_setup(pulled_models)


def _custom_model_setup(pulled_models: dict[str, float]) -> tuple[list[dict], str]:
    """
    Interactive custom model configuration.
    User adds models one by one with name, size, role, capabilities, description.
    """
    console.print("\n[bold cyan]Custom Model Configuration[/bold cyan]")
    console.print(
        "Add models one by one. For each model, enter its Ollama name "
        "(e.g. [cyan]qwen3:4b[/cyan]), size, role, and capabilities.\n"
        "You can copy model names from [link=https://ollama.com/library]ollama.com/library[/link].\n"
    )

    if pulled_models:
        console.print("[bold]Already pulled in Ollama:[/bold]")
        for name, size in pulled_models.items():
            console.print(f"  [cyan]{name}[/cyan] ({size:.1f} GB)")
        console.print()

    available_roles = ["instruct", "reasoning", "coding", "fast", "vision", "general"]
    models: list[dict] = []

    console.print("[dim]Type 'done' when finished adding models.[/dim]\n")

    while True:
        console.print(f"[bold]Model #{len(models) + 1}[/bold]")
        name = Prompt.ask("  Model name (from Ollama, e.g. qwen3:4b)")
        if name.strip().lower() in ("done", "exit", "quit", ""):
            if not models:
                console.print("[yellow]You need at least one model. Try again.[/yellow]")
                continue
            break

        # Try to auto-detect size from pulled models
        default_size = ""
        if name in pulled_models:
            default_size = str(pulled_models[name])
        size_str = Prompt.ask("  Size in GB", default=default_size or "2.5")
        try:
            size_gb = float(size_str)
        except ValueError:
            console.print("[yellow]Invalid size, defaulting to 2.5 GB[/yellow]")
            size_gb = 2.5

        console.print(f"  Available roles: {', '.join(available_roles)}")
        role = Prompt.ask("  Role", default="instruct" if not models else "general")

        caps = "text"
        if role == "vision" or Confirm.ask("  Is this a vision/multimodal model?", default=False):
            caps = "text,images"

        notes = Prompt.ask("  Description / notes", default="")

        auto_pull = Confirm.ask("  Auto-pull allowed?", default=True)

        model_entry = {
            "model": name.strip(),
            "size_gb": size_gb,
            "role": role.strip(),
            "capabilities": caps,
            "notes": notes.strip(),
            "auto_pull": auto_pull,
        }
        models.append(model_entry)

        console.print(f"  [green]Added {name} as {role}[/green]\n")

        if not Confirm.ask("  Add another model?", default=True):
            break

    # Pick default model
    console.print("\n[bold]Choose default model for the main agent loop:[/bold]")
    for i, m in enumerate(models):
        console.print(f"  [{i + 1}] {m['model']} ({m['size_gb']}GB, {m['role']})")

    instruct_models = [m for m in models if m["role"] == "instruct"]
    default_pick = instruct_models[0]["model"] if instruct_models else models[0]["model"]
    default_model = Prompt.ask("  Default model name", default=default_pick)

    return models, default_model


# ---------------------------------------------------------------------------
# Workspace location selection
# ---------------------------------------------------------------------------

def _select_workspace_location(default_workspace: Path) -> Path:
    """
    Ask the user where to store the workspace.  Runs the git safety check.
    Returns the confirmed workspace path.
    """
    platform_default = get_default_workspace()

    console.print("\n[bold]Step 1/6:[/bold] Workspace location\n")
    console.print(
        "  O.R.A. stores configuration, memory, and session data in a workspace\n"
        "  directory.  By default this is your OS user-data folder, which keeps\n"
        "  private files out of any git repository.\n"
    )
    console.print(f"  Default: [cyan]{platform_default}[/cyan]\n")

    while True:
        use_default = Confirm.ask("  Use this location?", default=True)

        if use_default:
            workspace_dir = platform_default
        else:
            raw = Prompt.ask("  Enter workspace path")
            workspace_dir = Path(raw).expanduser().resolve()

        # Run the git safety check (interactive — may raise WorkspaceRepick)
        try:
            safe = check_workspace_git_safety(workspace_dir, console)
        except WorkspaceRepick:
            console.print("[dim]Pick a different path...[/dim]\n")
            continue

        if not safe:
            console.print("[red]Setup aborted by user.[/red]")
            sys.exit(1)

        # Ensure the directory exists
        workspace_dir.mkdir(parents=True, exist_ok=True)
        ensure_inner_gitignore(workspace_dir)

        # Save the pointer so future runs find the workspace
        save_workspace_location(workspace_dir)
        console.print(f"  [green]Workspace: {workspace_dir}[/green]\n")
        return workspace_dir


# ---------------------------------------------------------------------------
# Original helpers (kept)
# ---------------------------------------------------------------------------

def _require_understand() -> None:
    """Display safety warning and require 'I UNDERSTAND' to continue."""
    console.print(Panel(SAFETY_WARNING, title="[bold red]SAFETY WARNING[/bold red]", border_style="red"))
    while True:
        answer = Prompt.ask("[bold red]Type 'I UNDERSTAND' to proceed[/bold red]")
        if answer.strip() == "I UNDERSTAND":
            break
        console.print("[red]You must type exactly: I UNDERSTAND[/red]")


def _pick_bootstrap_model(ollama_base_url: str, pulled_models: dict[str, float]) -> str:
    """Ask user to confirm/enter the bootstrap model."""
    if pulled_models:
        console.print("\n[bold]Pulled models in Ollama:[/bold]")
        for name, size in pulled_models.items():
            console.print(f"  {name} ({size:.1f} GB)")

    default = next(iter(pulled_models), "phi4-mini") if pulled_models else "phi4-mini"
    model = Prompt.ask(
        "\n[bold]Bootstrap model[/bold] (used for this wizard — must be already pulled)",
        default=default,
    )
    if pulled_models and model not in pulled_models:
        console.print(
            f"[yellow]Warning: '{model}' does not appear to be pulled in Ollama. "
            "The wizard may fail if it cannot reach this model.[/yellow]"
        )
    return model


def _wizard_chat(
    client: OpenAI,
    model: str,
    messages: list[dict],
    system_prompt: str,
) -> tuple[str, list[dict]]:
    """Send messages to the bootstrap model and return (reply_text, updated_messages)."""
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": system_prompt}] + messages
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        reply = response.choices[0].message.content or ""
        messages = messages + [{"role": "assistant", "content": reply}]
        return reply, messages
    except Exception as exc:
        return f"[model error: {exc}]", messages


def _interactive_wizard_session(
    bootstrap_model: str,
    ollama_base_url: str,
    pulled_models: dict[str, float],
    hardware_summary: str,
    workspace_dir: Path,
) -> tuple[str, str]:
    """
    Run an interactive prompt session with the bootstrap model to populate
    the user profile. Returns (user_name, wizard_notes).
    """
    client = OpenAI(
        base_url=ollama_base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    model_list = "\n".join(f"  - {n} ({s:.1f} GB)" for n, s in pulled_models.items()) or "  (none pulled)"

    system_prompt = (
        "You are helping to configure O.R.A. (Orchestrated Reasoning Agent) — a locally-hosted agentic AI system. "
        "Your job is to help the user set up their profile (name, timezone, working style, projects). "
        "Be concise and practical. Ask one thing at a time. "
        f"\n\nHardware: {hardware_summary}"
        f"\n\nPulled Ollama models:\n{model_list}"
    )

    messages: list[dict] = []

    console.print("\n[bold cyan]O.R.A. Wakeup Wizard — User Profile[/bold cyan]")
    console.print("[dim]The bootstrap model will help set up your user profile.[/dim]")
    console.print("[dim]Type 'done' when you are satisfied.[/dim]\n")

    opening = (
        "Hello! I'm going to help you set up your user profile for O.R.A. "
        "First, what's your name?"
    )
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

        # Extract user name from early messages (heuristic)
        if user_name == "User" and messages:
            for msg in messages[:6]:
                if msg.get("role") == "user":
                    words = msg["content"].split()
                    if len(words) <= 4 and words:
                        candidate = words[0].rstrip(",.!?").capitalize()
                        if candidate.isalpha() and len(candidate) >= 2:
                            user_name = candidate

        console.print(f"\n[bold cyan]Ora[/bold cyan]: {reply}\n")

    return user_name, ""


# ---------------------------------------------------------------------------
# Workspace file writers
# ---------------------------------------------------------------------------

def _write_workspace_files(
    workspace_dir: Path,
    bootstrap_model: str,
    default_model: str,
    ollama_base_url: str,
    user_name: str,
    models: list[dict],
) -> None:
    """Write all initial workspace markdown files."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "memory").mkdir(parents=True, exist_ok=True)

    # config.md
    (workspace_dir / "config.md").write_text(
        DEFAULT_CONFIG_CONTENT.format(
            base_url=ollama_base_url,
            bootstrap_model=bootstrap_model,
            default_model=default_model,
        ),
        encoding="utf-8",
    )

    # user_profile.md
    (workspace_dir / "user_profile.md").write_text(
        DEFAULT_USER_PROFILE.format(name=user_name),
        encoding="utf-8",
    )

    # viable_models.md
    write_viable_models(models, workspace_dir)

    # model_roles.md
    roles_content = _build_model_roles(models, bootstrap_model)
    (workspace_dir / "model_roles.md").write_text(roles_content, encoding="utf-8")

    # vision_config.md
    (workspace_dir / "vision_config.md").write_text(DEFAULT_VISION_CONFIG, encoding="utf-8")

    # session_state.md (blank initial)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (workspace_dir / "session_state.md").write_text(
        f"# Session State\n_Last updated: {now}_\n\n"
        f"## Active model\nmodel: {default_model}\ncontext_window: 32768\n"
        "tokens_used: 0\ntokens_used_pct: 0.0%\noverflow_threshold_pct: 82.0%\n\n"
        "## Switch log\n"
        "| time     | from              | to                  | reason                              |\n"
        "|----------|-------------------|---------------------|-------------------------------------|\n\n"
        "## Vision activity (this session)\n"
        "| time     | file              | strategy             | vision model     | instruct model |\n"
        "|----------|-------------------|----------------------|------------------|----------------|\n",
        encoding="utf-8",
    )

    # memory/context_summary.md (empty)
    (workspace_dir / "memory" / "context_summary.md").write_text(
        "# Context Summary\n_No sessions yet._\n\n## Summary\n(none)\n",
        encoding="utf-8",
    )

    # memory/persistent_memory.md
    (workspace_dir / "memory" / "persistent_memory.md").write_text(
        f"# Persistent Memory\n\n## Facts\n"
        f"- Setup completed: {now}\n"
        f"- Bootstrap model: {bootstrap_model}\n"
        f"- Default model: {default_model}\n"
        f"- Ollama base URL: {ollama_base_url}\n\n"
        "## Completed milestones\n"
        f"- {now[:10]}: First boot wizard completed\n\n"
        "## Notes from agent\n(none yet)\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main wizard entry point
# ---------------------------------------------------------------------------

def run_wizard(workspace_dir: Path, ollama_base_url: str = "http://127.0.0.1:11434") -> Path:
    """
    Execute the full first-run wakeup wizard.
    Writes all workspace files and returns the final workspace directory
    (which may differ from the input if the user picked a custom path).
    """
    # Step 0: Safety warning
    _require_understand()

    # Step 1: Workspace location (may change workspace_dir)
    workspace_dir = _select_workspace_location(workspace_dir)

    # Step 2: Check Ollama connectivity
    console.print("\n[bold]Step 2/6:[/bold] Checking Ollama")
    ollama_ok = False
    pulled_models: dict[str, float] = {}
    try:
        import urllib.request
        with urllib.request.urlopen(ollama_base_url.rstrip("/") + "/api/tags", timeout=5) as resp:
            if resp.status == 200:
                ollama_ok = True
    except Exception:
        pass

    if ollama_ok:
        console.print(f"[green]Ollama is reachable at {ollama_base_url}[/green]")
        pulled_models = query_ollama_models(ollama_base_url)
        if pulled_models:
            console.print(f"Found {len(pulled_models)} model(s): {', '.join(pulled_models.keys())}")
        else:
            console.print("[yellow]Ollama is running but no models are pulled yet.[/yellow]")
            console.print("[yellow]You can still configure models — they'll be pulled on first use.[/yellow]")
    else:
        console.print(
            Panel(
                f"Cannot reach Ollama at [bold]{ollama_base_url}[/bold].\n\n"
                "O.R.A. requires Ollama to run LLMs locally.\n"
                "  1. Install Ollama:  [cyan]curl -fsSL https://ollama.com/install.sh | sh[/cyan]\n"
                "  2. Start Ollama:    [cyan]ollama serve[/cyan]\n"
                "  3. Pull a model:    [cyan]ollama pull qwen3:4b[/cyan]\n\n"
                "You can continue setup without Ollama — model configuration and user profile\n"
                "will be saved, but the interactive wizard chat will not work.",
                title="[bold yellow]Ollama not found[/bold yellow]",
                border_style="yellow",
            )
        )
        if not Confirm.ask("Continue setup without Ollama?", default=True):
            console.print("[red]Setup cancelled. Start Ollama and run again.[/red]")
            sys.exit(1)

    # Step 3: Bootstrap model
    console.print("\n[bold]Step 3/6:[/bold] Bootstrap model selection")
    bootstrap_model = _pick_bootstrap_model(ollama_base_url, pulled_models)

    # Step 4: Hardware probe
    console.print("\n[bold]Step 4/6:[/bold] Hardware detection")
    hardware_summary, _ = probe_hardware(workspace_dir, ollama_base_url)
    console.print(f"[dim]{hardware_summary}[/dim]")

    # Step 5: Hardware tier selection / custom model setup
    console.print("\n[bold]Step 5/6:[/bold] Model configuration")
    models, default_model = _select_hardware_tier(pulled_models)

    # Step 6: Interactive user profile session with bootstrap model
    console.print("\n[bold]Step 6/6:[/bold] User profile setup\n")
    user_name, _ = _interactive_wizard_session(
        bootstrap_model, ollama_base_url, pulled_models, hardware_summary, workspace_dir,
    )

    # Write all workspace files
    _write_workspace_files(
        workspace_dir, bootstrap_model, default_model, ollama_base_url,
        user_name, models,
    )

    # Re-probe hardware with viable_models.md now in place
    probe_hardware(workspace_dir, ollama_base_url)

    console.print(
        Panel(
            f"Configuration saved to [bold]{workspace_dir}[/bold].\n"
            f"Default model: [cyan]{default_model or '(none — will prompt at startup)'}[/cyan]\n"
            f"Models configured: {len(models)}\n"
            "Launching O.R.A...",
            title="[bold green]Setup Complete[/bold green]",
            border_style="green",
        )
    )

    return workspace_dir
