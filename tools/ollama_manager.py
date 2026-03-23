"""
ollama_manager.py — list_models() and pull_model() tools.
list_models: returns viable_models.md with live hardware fit scores.
pull_model:  pulls a model from Ollama registry after validation.
"""
import subprocess
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm

from tools.hardware_probe import parse_viable_models, score_models, query_ollama_models


def _read_hardware_profile(workspace_dir: Path) -> tuple[float, float]:
    """Return (avail_vram_gb, avail_ram_gb) from hardware_profile.md."""
    path = workspace_dir / "hardware_profile.md"
    if not path.exists():
        return 0.0, 0.0
    avail_vram = 0.0
    avail_ram = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("vram_available_gb:"):
            try:
                avail_vram += float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("ram_available_gb:"):
            try:
                avail_ram = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return avail_vram, avail_ram


def list_models(workspace_dir, console: Console | None = None, ollama_base_url: str = "http://127.0.0.1:11434") -> str:
    """
    Return the viable models list with live hardware fit scores.
    This is callable as a LangChain tool — returns a plain string.
    """
    workspace_dir = Path(workspace_dir)
    viable = parse_viable_models(workspace_dir)
    avail_vram, avail_ram = _read_hardware_profile(workspace_dir)
    scored = score_models(viable, avail_vram, avail_ram)

    if not scored:
        return "No viable models defined yet. Edit workspace/viable_models.md to add models."

    pulled = query_ollama_models(ollama_base_url)

    lines = ["Viable models (live hardware fit scores):\n"]
    lines.append(f"{'Model':<28} {'Size':>7}  {'VRAM fit':>8}  {'RAM fit':>7}  {'Pulled':>6}  Role")
    lines.append("-" * 80)
    for m in scored:
        vram_sym = "yes" if m["fits_vram"] else "no"
        ram_sym = "yes" if m["fits_ram"] else "no"
        is_pulled = "yes" if m["model"] in pulled else "no"
        lines.append(
            f"{m['model']:<28} {m['size_gb']:>6.1f}  {vram_sym:>8}  {ram_sym:>7}  {is_pulled:>6}  {m['role']}"
        )
    return "\n".join(lines)


def pull_model(
    model_name: str,
    workspace_dir,
    ollama_base_url: str = "http://127.0.0.1:11434",
    console: Console | None = None,
) -> str:
    """
    Pull a model from Ollama's registry.

    Validates:
    - Model is listed in viable_models.md with auto_pull: yes
    - Model fits within available VRAM or RAM
    - User confirms (always required for pulls)
    """
    workspace_dir = Path(workspace_dir)
    console = console or Console()
    viable = parse_viable_models(workspace_dir)
    avail_vram, avail_ram = _read_hardware_profile(workspace_dir)

    # Find model in viable list
    match = next((m for m in viable if m["model"] == model_name), None)
    if match is None:
        return (
            f"Error: '{model_name}' is not in workspace/viable_models.md. "
            "Add it to the list before pulling."
        )

    if not match["auto_pull"]:
        return (
            f"Error: '{model_name}' has auto_pull: no. "
            "Set auto_pull: yes in viable_models.md to allow automatic pulling."
        )

    # Hardware fit check
    fits_vram = avail_vram >= match["size_gb"]
    fits_ram = avail_ram >= match["size_gb"]
    if not fits_vram and not fits_ram:
        return (
            f"Error: '{model_name}' ({match['size_gb']} GB) does not fit in "
            f"available VRAM ({avail_vram:.1f} GB) or RAM ({avail_ram:.1f} GB). "
            "Free up memory before pulling."
        )

    # User confirmation — always required for pulls
    console.print(
        f"\n[bold yellow]PULL REQUEST[/bold yellow] — This will download [cyan]{model_name}[/cyan] "
        f"(~{match['size_gb']} GB)."
    )
    if not Confirm.ask("Proceed with download?", default=False):
        return f"Pull cancelled by user."

    # Execute pull
    console.print(f"[dim]Pulling {model_name} via Ollama...[/dim]")
    try:
        result = subprocess.run(
            ["ollama", "pull", model_name],
            timeout=3600,  # 1 hour max
        )
        if result.returncode != 0:
            return f"Error: 'ollama pull {model_name}' failed (exit {result.returncode})."
    except FileNotFoundError:
        return "Error: 'ollama' binary not found. Is Ollama installed?"
    except subprocess.TimeoutExpired:
        return f"Error: Pull timed out after 1 hour."

    return f"Successfully pulled {model_name}."


def write_viable_models(models: list[dict], workspace_dir: Path) -> None:
    """
    Write workspace/viable_models.md from a list of model dicts.
    Each dict should have: model, size_gb, role, capabilities, notes, auto_pull.
    """
    lines = [
        "# Viable Models\n",
        "## Format",
        "Each entry: model name | size | role | capabilities | notes | auto-pull\n",
        "| model                      | size_gb | role      | capabilities  | notes                          | auto_pull |",
        "|----------------------------|---------|-----------|---------------|--------------------------------|-----------|",
    ]
    for m in models:
        name = m.get("model", "")
        size = m.get("size_gb", 0)
        role = m.get("role", "general")
        caps = m.get("capabilities", "text")
        notes = m.get("notes", "")
        auto = "yes" if m.get("auto_pull", True) else "no"
        lines.append(
            f"| {name:<26} | {size:<7} | {role:<9} | {caps:<13} | {notes:<30} | {auto:<9} |"
        )
    lines += [
        "",
        "## Notes",
        "- Models not in this list cannot be loaded by the agent.",
        "- Set auto_pull: no to prevent the agent from downloading a model without explicit user action.",
        "- The agent will never load a model that does not fit in available VRAM+RAM.",
        "- capabilities: text = text only, text,images = multimodal vision model.",
    ]
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "viable_models.md").write_text("\n".join(lines), encoding="utf-8")


def write_initial_viable_models(pulled_models: dict[str, float], workspace_dir: Path) -> None:
    """
    Write workspace/viable_models.md from models currently pulled in Ollama.
    Called during the first-run wizard (legacy fallback — presets use write_viable_models).
    """
    models = []
    for name, size in pulled_models.items():
        models.append({
            "model": name,
            "size_gb": size,
            "role": "general",
            "capabilities": "text",
            "notes": "auto-detected on first run",
            "auto_pull": True,
        })
    write_viable_models(models, workspace_dir)
