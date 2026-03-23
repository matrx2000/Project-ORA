"""
model_switcher.py — switch_model(role, task_prompt, transfer_context) tool.
Delegates a sub-task to a specialist model (local or remote) and returns its response.
"""
import datetime
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.prompt import Confirm


# Remote models get this system prompt prefix — hard block on tool execution
_REMOTE_SYSTEM_PREFIX = (
    "You are a specialist text-generation assistant for O.R.A.\n"
    "Your output is plain text only.\n"
    "You must not output tool calls, JSON function calls, bash commands, or any "
    "instruction for the host system to execute. Any such output will be discarded.\n\n"
)


def _parse_model_roles(workspace_dir: Path) -> dict[str, dict]:
    """
    Parse workspace/model_roles.md into {role_name: {model, use_when, ...}}.
    """
    # Try models.md first (new format), fall back to model_roles.md (legacy)
    path = workspace_dir / "models.md"
    if not path.exists():
        path = workspace_dir / "model_roles.md"
    if not path.exists():
        return {}

    roles: dict[str, dict] = {}
    current_role: str | None = None
    current_data: dict = {}
    multiline_key: str | None = None
    multiline_parts: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if stripped.startswith("### "):
            # Flush previous role
            if current_role is not None:
                if multiline_key:
                    current_data[multiline_key] = " ".join(multiline_parts)
                    multiline_key = None
                    multiline_parts = []
                roles[current_role] = current_data
            current_role = stripped[4:].strip()
            current_data = {}

        elif stripped.startswith("#"):
            continue

        elif current_role is not None:
            # Continuation of a block scalar (use_when: >)
            if multiline_key and (line.startswith("  ") or line.startswith("\t")):
                multiline_parts.append(stripped)
                continue

            # Flush any pending multiline
            if multiline_key:
                current_data[multiline_key] = " ".join(multiline_parts)
                multiline_key = None
                multiline_parts = []

            if ": >" in stripped:
                key = stripped.split(": >")[0].strip()
                multiline_key = key
                multiline_parts = []
            elif ": " in stripped:
                key, value = stripped.split(": ", 1)
                current_data[key.strip()] = value.strip()

    # Flush last role
    if current_role is not None:
        if multiline_key:
            current_data[multiline_key] = " ".join(multiline_parts)
        roles[current_role] = current_data

    return roles


def _read_hardware_fit(workspace_dir: Path, model_name: str) -> tuple[bool, str]:
    """
    Check if model_name fits in available VRAM or RAM.
    Returns (fits: bool, reason: str).
    """
    from tools.hardware_probe import parse_viable_models, score_models

    avail_vram = 0.0
    avail_ram = 0.0
    hw_path = workspace_dir / "hardware_profile.md"
    if hw_path.exists():
        for line in hw_path.read_text(encoding="utf-8").splitlines():
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

    viable = parse_viable_models(workspace_dir)
    match = next((m for m in viable if m["model"] == model_name), None)
    if match is None:
        return False, f"'{model_name}' is not in viable_models.md"

    scored = score_models([match], avail_vram, avail_ram)[0]
    if scored["fits_vram"]:
        return True, f"fits VRAM ({avail_vram:.1f} GB free)"
    if scored["fits_ram"]:
        return True, f"fits RAM ({avail_ram:.1f} GB free, no GPU)"
    return False, (
        f"does not fit — model needs {match['size_gb']} GB, "
        f"VRAM free: {avail_vram:.1f} GB, RAM free: {avail_ram:.1f} GB"
    )


def _append_switch_log(workspace_dir: Path, from_model: str, to_model: str, reason: str) -> None:
    """Append a row to the switch log in session_state.md."""
    path = workspace_dir / "session_state.md"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    new_row = f"| {timestamp} | {from_model:<17} | {to_model:<19} | {reason:<35} |"
    # Find the switch log table and append
    if "## Switch log" in content:
        content = content + "\n" + new_row
    path.write_text(content, encoding="utf-8")


def _find_approved_remote(role, session_decisions, scored_remote_models):
    """Find the best approved remote model for a role, or None."""
    if not scored_remote_models or not session_decisions:
        return None
    for s in scored_remote_models:
        if s.role != role:
            continue
        key = (s.node_label, s.model)
        if session_decisions.get(key) == "approved" and s.is_better:
            return s
    return None


def _call_remote_model(node_address, model_name, role, task_prompt, transfer_context):
    """Call a remote Ollama model. Returns (success: bool, result: str)."""
    import httpx

    base_url = f"http://{node_address}"
    system_content = (
        _REMOTE_SYSTEM_PREFIX
        + f"Role: {role}.\n"
        + f"Context from primary agent:\n{transfer_context}"
    )
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": task_prompt},
        ],
        "stream": False,
        "keep_alive": "0s",
    }
    try:
        resp = httpx.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Extract only .content — never parse tool calls from remote
            content = data.get("message", {}).get("content", "")
            return True, content
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def make_switch_model_tool(
    workspace_dir,
    ollama_base_url: str,
    active_model_ref: list,  # mutable single-element list so tool sees current value
    require_confirm: bool,
    console: Console | None,
    session_decisions: dict | None = None,
    scored_remote_models: list | None = None,
):
    """
    Factory that returns a switch_model function bound to current session state.
    active_model_ref is a list([model_name]) so the tool always reads the live value.

    session_decisions: {(node_label, model): "approved"|"declined"} from network scan
    scored_remote_models: list of ScoredRemoteModel from network scan

    console may be None (TUI mode). In that case, status messages are skipped
    and confirmation prompts are auto-approved.
    """
    workspace_dir = Path(workspace_dir)
    _session_decisions = session_decisions or {}
    _scored_remote = scored_remote_models or []

    def _print(msg: str) -> None:
        if console is not None:
            console.print(msg)

    def switch_model(role: str, task_prompt: str, transfer_context: str) -> str:
        """
        Delegate a sub-task to a specialist model.

        Args:
            role: One of 'reasoning', 'coding', 'fast' (or any role in model_roles.md).
            task_prompt: The specific question or task for the specialist.
            transfer_context: Compact summary of relevant prior context (<=500 tokens).

        Returns:
            The specialist model's response as a string.
        """
        roles = _parse_model_roles(workspace_dir)
        if role not in roles:
            available = ", ".join(roles.keys()) or "none defined"
            return f"Error: unknown role '{role}'. Available roles: {available}"

        target_model = roles[role].get("model", "")
        if not target_model:
            return f"Error: role '{role}' has no 'model' field in model_roles.md"

        current_model = active_model_ref[0]

        # Check if a better approved remote model exists for this role
        remote_candidate = _find_approved_remote(role, _session_decisions, _scored_remote)
        use_remote = remote_candidate is not None

        if use_remote:
            remote_model = remote_candidate.model
            remote_node = remote_candidate.node_label
            remote_addr = remote_candidate.node_address

            if require_confirm and console is not None:
                _print(
                    f"\n[bold yellow]MODEL SWITCH (remote)[/bold yellow] "
                    f"-> [cyan]{remote_model}[/cyan] on {remote_node} "
                    f"(role: {role})"
                )
                if not Confirm.ask("Allow switch?", default=True):
                    use_remote = False

        if use_remote:
            _print(
                f"[dim]  [switch] {current_model} -> {remote_model}@{remote_node} "
                f"(role: {role}, remote)[/dim]"
            )
            ok, result = _call_remote_model(
                remote_addr, remote_model, role, task_prompt, transfer_context
            )
            if ok:
                _append_switch_log(
                    workspace_dir, current_model,
                    f"{remote_model}@{remote_node}",
                    f"{role} role (remote)",
                )
                _print(
                    f"[dim]  [switch] {remote_model}@{remote_node} returned result, "
                    f"back to {current_model}[/dim]"
                )
                return result

            _print(
                f"[yellow]  [switch] Remote node '{remote_node}' is not responding. "
                f"Falling back to local {target_model}.[/yellow]"
            )

        # Local model path
        if require_confirm and console is not None:
            _print(
                f"\n[bold yellow]MODEL SWITCH[/bold yellow] "
                f"[dim]{current_model}[/dim] -> [cyan]{target_model}[/cyan] "
                f"(role: {role})"
            )
            if not Confirm.ask("Allow switch?", default=True):
                return "Model switch cancelled by user."

        _print(
            f"[dim]  [switch] {current_model} -> {target_model} (role: {role})[/dim]"
        )

        # Call specialist via Ollama's OpenAI-compatible endpoint
        client = OpenAI(
            base_url=ollama_base_url.rstrip("/") + "/v1",
            api_key="ollama",
        )
        try:
            response = client.chat.completions.create(
                model=target_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are a specialist assistant. "
                            f"Role: {role}. "
                            f"Context from primary agent:\n{transfer_context}"
                        ),
                    },
                    {"role": "user", "content": task_prompt},
                ],
                extra_body={"keep_alive": "0s"},  # unload immediately after call
            )
        except Exception as exc:
            return f"Error calling specialist model {target_model}: {exc}"

        result = response.choices[0].message.content or ""

        # Log the switch
        _append_switch_log(workspace_dir, current_model, target_model, f"{role} role requested")

        _print(
            f"[dim]  [switch] {target_model} returned result, back to {current_model}[/dim]"
        )
        return result

    return switch_model
