"""
main.py — O.R.A. entry point.
Boot sequence + LangGraph ReAct agent loop + /settings mode.
"""
import sys
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool as lc_tool
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

# Local modules — add project directory to sys.path so tools/ resolves correctly
sys.path.insert(0, str(Path(__file__).parent))

from boot import run_wizard
from bash_tool import make_run_bash_tool
from tools.hardware_probe import probe_hardware
from tools.model_switcher import make_switch_model_tool
from tools.ollama_manager import list_models as _list_models, pull_model as _pull_model
from tools.context_manager import check_and_compact, get_token_stats
from tools.network_scanner import run_network_scan, ScoredRemoteModel, NetworkConfig
from tools.vision_router import route_user_message, VisionResult
from tools.workspace_resolver import (
    resolve_workspace, save_workspace_location, run_silent_safety_check,
    get_config_dir,
)

console = Console()

BRIEF_SAFETY_WARNING = (
    "[bold red]WARNING:[/bold red] O.R.A. has unrestricted Linux access. "
    "Run only on a dedicated machine."
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class OraConfig:
    ollama_base_url: str = "http://127.0.0.1:11434"
    default_model: str = ""
    overflow_threshold: float = 0.82
    summary_keep_last_n_turns: int = 4
    max_summary_tokens: int = 400
    allow_agent_initiated_switching: bool = True
    require_user_confirm_switch: bool = False
    bash_exclude_commands: list = field(default_factory=list)
    bash_require_confirm: bool = True
    bash_restrict_to_workspace: bool = True
    bash_warn_destructive: bool = True
    auto_save_session_state: bool = True
    auto_reload_config: bool = False


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "1")


def load_config(workspace_dir: Path) -> OraConfig:
    config_path = workspace_dir / "config.md"
    cfg = OraConfig()
    if not config_path.exists():
        return cfg

    raw: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ": " in line:
            key, value = line.split(": ", 1)
            raw[key.strip()] = value.strip()

    if "base_url" in raw:
        cfg.ollama_base_url = raw["base_url"]
    if "default_model" in raw:
        cfg.default_model = raw["default_model"]
    if "overflow_threshold" in raw:
        try:
            cfg.overflow_threshold = float(raw["overflow_threshold"])
        except ValueError:
            pass
    if "summary_keep_last_n_turns" in raw:
        try:
            cfg.summary_keep_last_n_turns = int(raw["summary_keep_last_n_turns"])
        except ValueError:
            pass
    if "max_summary_tokens" in raw:
        try:
            cfg.max_summary_tokens = int(raw["max_summary_tokens"])
        except ValueError:
            pass
    if "allow_agent_initiated_switching" in raw:
        cfg.allow_agent_initiated_switching = _parse_bool(raw["allow_agent_initiated_switching"])
    if "require_user_confirm_switch" in raw:
        cfg.require_user_confirm_switch = _parse_bool(raw["require_user_confirm_switch"])
    if "bash_exclude_commands" in raw:
        cfg.bash_exclude_commands = [
            s.strip() for s in raw["bash_exclude_commands"].split(",") if s.strip()
        ]
    if "bash_require_confirm" in raw:
        cfg.bash_require_confirm = _parse_bool(raw["bash_require_confirm"])
    if "bash_restrict_to_workspace" in raw:
        cfg.bash_restrict_to_workspace = _parse_bool(raw["bash_restrict_to_workspace"])
    if "bash_warn_destructive" in raw:
        cfg.bash_warn_destructive = _parse_bool(raw["bash_warn_destructive"])
    if "auto_save_session_state" in raw:
        cfg.auto_save_session_state = _parse_bool(raw["auto_save_session_state"])
    if "auto_reload_config" in raw:
        cfg.auto_reload_config = _parse_bool(raw["auto_reload_config"])

    return cfg


# ---------------------------------------------------------------------------
# Workspace file loaders
# ---------------------------------------------------------------------------

def _load_text(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _get_model_for_role(workspace_dir: Path, role: str) -> str:
    """Read the model assigned to a role from models.md."""
    path = workspace_dir / "models.md"
    if not path.exists():
        return ""
    current_role = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            current_role = stripped[4:].strip()
        elif current_role == role and stripped.startswith("model:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _build_system_prompt(
    workspace_dir: Path,
    hardware_summary: str,
    config: "OraConfig | None" = None,
    approved_remote_models: list[ScoredRemoteModel] | None = None,
) -> str:
    user_profile = _load_text(workspace_dir / "user_profile.md")
    model_roles = _load_text(workspace_dir / "models.md")
    persistent_memory = _load_text(workspace_dir / "memory" / "persistent_memory.md")
    context_summary = _load_text(workspace_dir / "memory" / "context_summary.md")

    # Build remote models section if any approved
    remote_section = ""
    if approved_remote_models:
        remote_lines = ["[remote models — approved for this session]"]
        for s in approved_remote_models:
            desc = s.description or "no description"
            remote_lines.append(
                f"- {s.model} on {s.node_label} ({s.role}): \"{desc}\""
            )
        remote_lines.append(
            "- Use switch_model(role=<role>) to route to remote models automatically "
            "when they are the best match for the role."
        )
        remote_section = "\n".join(remote_lines)

    cfg_dir = get_config_dir()

    # Build filesystem access rule based on config
    restrict = config.bash_restrict_to_workspace if config else True
    if restrict:
        fs_rule = (
            f"- IMPORTANT: bash_restrict_to_workspace is ON. You can only read/write files "
            f"inside the workspace ({workspace_dir}). Commands targeting paths outside the "
            f"workspace will be blocked. The user can disable this in /settings safety."
        )
    else:
        fs_rule = (
            "- bash_restrict_to_workspace is OFF. You have full access to the Linux "
            "filesystem. Be careful with system files and always confirm destructive actions."
        )

    return f"""You are O.R.A. (Orchestrated Reasoning Agent) — an autonomous local AI agent running on Linux via Ollama.
O.R.A. stands for Orchestrated Reasoning Agent. You are not affiliated with any existing brand or product. You are O.R.A.

[file locations]
workspace: {workspace_dir}
config pointer: {cfg_dir}
memory: {workspace_dir / "memory"}

[user]
{user_profile}

[previous session summary]
{context_summary}

[hardware]
{hardware_summary}

[available models and roles]
{model_roles}

{remote_section}

[persistent memory]
{persistent_memory}

[tools]
- run_bash(command): execute a Linux shell command (requires user confirmation).
  To read files use: cat <path>
  To write files use: echo "content" > <path>  or  tee <path> for multi-line
  To append use: echo "content" >> <path>
  To create directories use: mkdir -p <path>
- switch_model(role, task_prompt, transfer_context): delegate a sub-task to a specialist
  model. role must be one of the roles defined in models.md. Write transfer_context
  in <=500 tokens — only what the specialist needs to know. Their response returns as a
  tool result.
- list_models(): show current model-to-role assignments from models.md
- pull_model(model_name): pull a new model from Ollama (requires user confirmation)
- show_paths(): show where O.R.A. stores workspace, config, and memory files on this system

[rules]
- Always use run_bash for shell commands — never assume a command ran without calling it.
{fs_rule}
- Prefer VRAM-fit models when switching. Only use RAM-only models if no VRAM model fits.
- Do not switch models for trivial tasks. Switch only when the role description matches.
- Keep transfer_context concise (<=500 tokens). Do not dump the full conversation history.
- When the user tells you their name, preferences, timezone, projects, or any personal
  info, UPDATE {workspace_dir}/user_profile.md immediately using run_bash. Read the file
  first (cat), then rewrite it with the updated content (tee). This is how you remember
  who the user is across sessions.
- You may append general facts to {workspace_dir}/memory/persistent_memory.md when you
  learn something worth remembering across sessions (not user profile info — that goes
  in user_profile.md).
- When the user types /settings, enter settings mode to help configure workspace files.
"""


# ---------------------------------------------------------------------------
# Settings mode
# ---------------------------------------------------------------------------

SETTINGS_SYSTEM_PROMPT = """\
You are O.R.A. in settings mode. Your only job is to help the user read and
modify workspace configuration files. You may only read and write files in
the workspace/ directory. You must always show a plain-language diff and
receive explicit confirmation before writing any file. Never run bash commands
or call any model-switching tools. When the user types /done or /exit, settings
mode ends and normal operation resumes.

Available workspace files you can read and modify:
- workspace/config.md — main agent config (models, safety, context, session settings)
- workspace/network_config.md — remote Ollama nodes and model descriptions
- workspace/network_trust.md — remembered trust decisions for remote models
- workspace/models.md — model-to-role assignments (single source of truth)
- workspace/user_profile.md — user name, timezone, preferences
- workspace/memory/persistent_memory.md — long-term facts and notes

Files you CANNOT modify (auto-generated):
- workspace/hardware_profile.md
- workspace/network_registry.md
- workspace/session_state.md
"""


def _parse_settings_focus(user_input: str) -> str | None:
    """Parse '/settings [focus]' and return the focus area, or None."""
    parts = user_input.strip().split(maxsplit=1)
    if len(parts) > 1:
        return parts[1].lower()
    return None


_SETTINGS_CONTEXT_FILES = {
    "network": ["network_config.md", "network_trust.md"],
    "models": ["models.md"],
    "profile": ["user_profile.md"],
    "safety": ["config.md"],
    "memory": ["config.md", "memory/persistent_memory.md"],
    "vision": ["vision_config.md", "models.md"],
}

# Auto-generated files that settings mode must not write to
_SETTINGS_READONLY_FILES = {"hardware_profile.md", "network_registry.md", "session_state.md"}


def _resolve_settings_files(user_input: str, settings_focus: str | None) -> list[str]:
    """Decide which workspace files to read based on focus or keywords."""
    if settings_focus and settings_focus in _SETTINGS_CONTEXT_FILES:
        return _SETTINGS_CONTEXT_FILES[settings_focus]
    lower = user_input.lower()
    if any(kw in lower for kw in ("network", "remote", "node", "trust")):
        return _SETTINGS_CONTEXT_FILES["network"]
    if any(kw in lower for kw in ("model", "role", "viable", "switch")):
        return _SETTINGS_CONTEXT_FILES["models"]
    if any(kw in lower for kw in ("profile", "name", "timezone", "working style")):
        return _SETTINGS_CONTEXT_FILES["profile"]
    if any(kw in lower for kw in ("safety", "bash", "block", "confirm")):
        return _SETTINGS_CONTEXT_FILES["safety"]
    if any(kw in lower for kw in ("memory", "fact", "persistent", "summary", "overflow")):
        return _SETTINGS_CONTEXT_FILES["memory"]
    if any(kw in lower for kw in ("vision", "image", "multimodal")):
        return _SETTINGS_CONTEXT_FILES["vision"]
    return ["config.md"]


def _read_workspace_files(workspace_dir: Path, files: list[str]) -> dict[str, str]:
    result = {}
    for f in files:
        p = workspace_dir / f
        if p.exists():
            result[f] = p.read_text(encoding="utf-8")
    return result


def _extract_file_block(reply: str) -> tuple[str, str] | None:
    """
    Look for a fenced code block tagged with a workspace filename in the LLM reply.
    Format the LLM is instructed to use:
        ```file:workspace/<filename>
        ...new file contents...
        ```
    Returns (relative_filename, content) or None.
    """
    import re
    match = re.search(
        r'```file:workspace/(\S+)\n(.*?)```',
        reply, re.DOTALL,
    )
    if match:
        return match.group(1), match.group(2)
    return None


def _run_settings_turn(
    user_input: str,
    workspace_dir: Path,
    active_model: str,
    ollama_base_url: str,
    settings_messages: list[dict],
    settings_focus: str | None,
) -> tuple[str, list[dict]]:
    """
    Handle one turn of settings mode conversation.
    Uses the active model to interpret the user's intent and propose changes.
    If the model proposes a file write, ask user to confirm, then write it.
    Returns (reply_text, updated_messages).
    """
    from openai import OpenAI

    client = OpenAI(
        base_url=ollama_base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    files_to_read = _resolve_settings_files(user_input, settings_focus)
    file_contents = _read_workspace_files(workspace_dir, files_to_read)

    file_context = ""
    for fname, content in file_contents.items():
        file_context += f"\n--- Current contents of workspace/{fname} ---\n{content}\n"

    settings_messages.append({"role": "user", "content": user_input})

    sys_content = (
        SETTINGS_SYSTEM_PROMPT
        + "\n\nWhen you need to write a changed file, output the FULL new file contents "
        "inside a fenced code block tagged with the filename, like:\n"
        "```file:workspace/config.md\n"
        "...full file contents...\n"
        "```\n"
        "Always show a short summary of what will change BEFORE the code block.\n"
    )

    api_messages = [
        {"role": "system", "content": sys_content},
    ] + settings_messages + [
        {"role": "system", "content": f"[Current file state for reference]{file_context}"},
    ]

    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=api_messages,
        )
        reply = response.choices[0].message.content or ""
    except Exception as exc:
        reply = f"[settings error: {exc}]"

    settings_messages.append({"role": "assistant", "content": reply})

    # Check if the reply contains a file write proposal
    file_block = _extract_file_block(reply)
    if file_block:
        filename, new_content = file_block
        if filename in _SETTINGS_READONLY_FILES:
            console.print(f"[red][ora/settings] Cannot modify {filename} — it is auto-generated.[/red]")
        else:
            console.print(f"\n[bold yellow][ora/settings][/bold yellow] Proposed change to [cyan]workspace/{filename}[/cyan]")
            if Confirm.ask("  Apply this change?", default=False):
                target = workspace_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")
                console.print(f"  [green]workspace/{filename} updated.[/green]")
            else:
                console.print("  No changes made.")

    return reply, settings_messages


# ---------------------------------------------------------------------------
# Session state writer
# ---------------------------------------------------------------------------

def _write_session_state(
    workspace_dir: Path,
    active_model: str,
    messages: list,
    overflow_count: int = 0,
    vision_logs: list[dict] | None = None,
) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats = get_token_stats(messages, active_model)

    # Read existing switch log and vision log to preserve them
    state_path = workspace_dir / "session_state.md"
    switch_log_lines = []
    vision_log_lines = []
    if state_path.exists():
        content = state_path.read_text(encoding="utf-8")
        if "## Switch log" in content:
            section = content.split("## Switch log", 1)[1]
            # Stop at next section
            if "## Vision" in section:
                section = section.split("## Vision")[0]
            for line in section.splitlines():
                if line.strip().startswith("|"):
                    switch_log_lines.append(line)
        if "## Vision activity" in content:
            section = content.split("## Vision activity", 1)[1]
            for line in section.splitlines():
                if line.strip().startswith("|"):
                    vision_log_lines.append(line)

    switch_header = (
        "| time     | from              | to                  | reason                              |\n"
        "|----------|-------------------|---------------------|-------------------------------------|\n"
    )
    switch_body = "\n".join(switch_log_lines[2:]) if len(switch_log_lines) > 2 else ""

    vision_header = (
        "| time     | file              | strategy             | vision model     | instruct model |\n"
        "|----------|-------------------|----------------------|------------------|----------------|\n"
    )
    # Append new vision logs
    existing_vision = "\n".join(vision_log_lines[2:]) if len(vision_log_lines) > 2 else ""
    if vision_logs:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        new_rows = []
        for vl in vision_logs:
            new_rows.append(
                f"| {ts} | {vl['file']:<17} | {vl['strategy']:<20} | {vl['vision_model']:<16} | {vl['instruct_model']:<14} |"
            )
        if existing_vision:
            existing_vision += "\n" + "\n".join(new_rows)
        else:
            existing_vision = "\n".join(new_rows)

    content = (
        f"# Session State\n_Last updated: {now}_\n\n"
        f"## Active model\n"
        f"model: {active_model}\n"
        f"context_window: {stats['context_window']}\n"
        f"tokens_used: {stats['tokens_used']}\n"
        f"tokens_used_pct: {stats['tokens_used_pct']}%\n"
        f"overflow_threshold_pct: 82.0%\n"
        f"overflow_events_this_session: {overflow_count}\n\n"
        f"## Switch log\n{switch_header}{switch_body}\n\n"
        f"## Vision activity (this session)\n{vision_header}{existing_vision}\n"
    )
    state_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Save final session summary on exit
# ---------------------------------------------------------------------------

def _save_exit_summary(
    messages: list,
    active_model: str,
    config: OraConfig,
    workspace_dir: Path,
    overflow_count: int,
) -> None:
    from tools.context_manager import _summarise, _write_context_summary
    from langchain_core.messages import SystemMessage as SM

    non_system = [m for m in messages if not isinstance(m, SM)]
    if not non_system:
        return

    console.print("[dim][exit] Saving session summary...[/dim]")
    summary = _summarise(non_system, active_model, config.ollama_base_url, config.max_summary_tokens)
    _write_context_summary(workspace_dir, summary, overflow_count)
    _write_session_state(workspace_dir, active_model, messages, overflow_count)
    console.print("[dim][exit] Session saved.[/dim]")


# ---------------------------------------------------------------------------
# LangGraph agent builder
# ---------------------------------------------------------------------------

State = dict  # We manage messages externally; graph receives full history each call


# ---------------------------------------------------------------------------
# Streaming printer with <think> tag support
# ---------------------------------------------------------------------------

class ThinkingStreamPrinter:
    """
    Streams LLM output to the terminal token-by-token.
    Thinking blocks (<think>...</think>) are rendered in a distinct colour with
    a visible border so they stand out from the final response.
    """

    THINK_START = "<think>"
    THINK_END = "</think>"

    # ANSI codes
    RESET      = "\033[0m"
    BOLD       = "\033[1m"
    DIM        = "\033[2m"
    # Thinking style: dim magenta italic
    THINK_CLR  = "\033[2;3;35m"
    # Border for thinking block
    THINK_BAR  = "\033[35m"
    # Response style: normal white
    RESP_CLR   = "\033[0m"

    def __init__(self):
        self.in_think = False
        self.buffer = ""
        self.header_printed = False
        self.think_header_printed = False
        self.has_content = False

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _print_header(self) -> None:
        if not self.header_printed:
            self._write(f"\n{self.BOLD}Ora{self.RESET}: ")
            self.header_printed = True

    def _open_think_block(self) -> None:
        """Print the thinking section header with a coloured border."""
        if not self.think_header_printed:
            self._write(
                f"\n{self.THINK_BAR}  {'─' * 40}{self.RESET}\n"
                f"{self.THINK_BAR}  thinking ...{self.RESET}\n"
                f"{self.THINK_BAR}  {'─' * 40}{self.RESET}\n"
            )
            self.think_header_printed = True

    def _close_think_block(self) -> None:
        """Print the closing border for the thinking section."""
        if self.think_header_printed:
            self._write(
                f"\n{self.THINK_BAR}  {'─' * 40}{self.RESET}\n\n"
            )

    def feed(self, text: str) -> None:
        """Feed a chunk of streamed text. Handles <think> tag boundaries."""
        self.buffer += text
        self._process()

    def _process(self) -> None:
        while self.buffer:
            if self.in_think:
                idx = self.buffer.find(self.THINK_END)
                if idx == -1:
                    # Partial — emit all but last len(THINK_END)-1 chars
                    safe = len(self.buffer) - (len(self.THINK_END) - 1)
                    if safe > 0:
                        self.has_content = True
                        self._write(f"{self.THINK_CLR}{self.buffer[:safe]}{self.RESET}")
                        self.buffer = self.buffer[safe:]
                    return
                else:
                    chunk = self.buffer[:idx]
                    if chunk:
                        self.has_content = True
                        self._write(f"{self.THINK_CLR}{chunk}{self.RESET}")
                    self._close_think_block()
                    self.buffer = self.buffer[idx + len(self.THINK_END):]
                    self.in_think = False
            else:
                idx = self.buffer.find(self.THINK_START)
                if idx == -1:
                    safe = len(self.buffer) - (len(self.THINK_START) - 1)
                    if safe > 0:
                        self._print_header()
                        self.has_content = True
                        self._write(f"{self.RESP_CLR}{self.buffer[:safe]}")
                        self.buffer = self.buffer[safe:]
                    return
                else:
                    chunk = self.buffer[:idx]
                    if chunk:
                        self._print_header()
                        self.has_content = True
                        self._write(f"{self.RESP_CLR}{chunk}")
                    self._open_think_block()
                    self.has_content = True
                    self.buffer = self.buffer[idx + len(self.THINK_START):]
                    self.in_think = True

    def finish(self) -> None:
        """Flush remaining buffer and close any open styling."""
        if self.buffer:
            self.has_content = True
            if self.in_think:
                self._write(f"{self.THINK_CLR}{self.buffer}{self.RESET}")
                self._close_think_block()
            else:
                self._print_header()
                self._write(f"{self.RESP_CLR}{self.buffer}")
            self.buffer = ""
        if self.has_content:
            self._write(f"{self.RESET}\n\n")


# ---------------------------------------------------------------------------
# LangGraph agent builder
# ---------------------------------------------------------------------------

def _try_parse_text_tool_calls(message):
    """
    Fallback parser for models that emit tool calls as JSON text instead of
    using the structured tool_calls format.  Detects patterns like:
      [{"name": "run_bash", "arguments": {"command": "ls"}}]
    or embedded in markdown code fences, and converts them to real tool_calls
    on the AIMessage so LangGraph can route them.
    """
    import json as _json

    if not message or not hasattr(message, "content") or not message.content:
        return message
    # Already has real tool calls — nothing to do
    if hasattr(message, "tool_calls") and message.tool_calls:
        return message

    content = message.content.strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.splitlines()
        # Remove first and last fence lines
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        content = "\n".join(lines).strip()

    # Try to find a JSON array of tool calls anywhere in the content
    # Look for [{"name": ...}] pattern
    import re
    match = re.search(r'\[[\s\S]*?\{[\s\S]*?"name"[\s\S]*?\}[\s\S]*?\]', content)
    if not match:
        # Also try a single object: {"name": ..., "arguments": ...}
        match = re.search(r'\{[\s\S]*?"name"[\s\S]*?"arguments"[\s\S]*?\}', content)
        if match:
            # Wrap in array
            try:
                obj = _json.loads(match.group())
                candidates = [obj]
            except (ValueError, _json.JSONDecodeError):
                return message
        else:
            return message
    else:
        try:
            candidates = _json.loads(match.group())
        except (ValueError, _json.JSONDecodeError):
            return message

    if not isinstance(candidates, list):
        candidates = [candidates]

    parsed_calls = []
    for i, call in enumerate(candidates):
        if not isinstance(call, dict):
            continue
        name = call.get("name") or call.get("function", {}).get("name")
        args = call.get("arguments") or call.get("parameters") or call.get("function", {}).get("arguments", {})
        if not name:
            continue
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except (ValueError, _json.JSONDecodeError):
                args = {"input": args}
        parsed_calls.append({
            "name": name,
            "args": args if isinstance(args, dict) else {},
            "id": f"fallback_{i}",
            "type": "tool_call",
        })

    if not parsed_calls:
        return message

    # Replace the text content with the non-JSON portion (if any) and attach real tool_calls
    # Extract the text before the JSON as the actual content
    json_start = content.find(match.group())
    pre_text = content[:json_start].strip() if json_start > 0 else ""

    message.tool_calls = parsed_calls
    message.content = pre_text
    return message


def build_graph(llm_with_tools, tool_node, console: Console | None = None):
    """Build a LangGraph ReAct graph with streaming output and tool visibility."""
    from typing import TypedDict

    _con = console or Console()

    class AgentState(TypedDict):
        messages: Annotated[list, add_messages]

    graph = StateGraph(AgentState)

    def call_llm(state: AgentState) -> AgentState:
        printer = ThinkingStreamPrinter()
        full_response = None

        for chunk in llm_with_tools.stream(state["messages"]):
            if full_response is None:
                full_response = chunk
            else:
                full_response = full_response + chunk

            # Ollama returns thinking in a separate "thinking" field, not in
            # content. We wrap it in <think> tags for the printer to render.
            thinking = ""
            if hasattr(chunk, "thinking") and chunk.thinking:
                thinking = chunk.thinking
            elif hasattr(chunk, "additional_kwargs") and chunk.additional_kwargs:
                thinking = chunk.additional_kwargs.get("thinking", "")
            if thinking:
                if not printer.in_think:
                    printer.feed("<think>")
                printer.feed(thinking)
            elif chunk.content:
                # Close thinking block if we were in one and now got content
                if printer.in_think:
                    printer.feed("</think>")
                printer.feed(chunk.content)

        printer.finish()

        # Fallback: some local models emit tool calls as JSON text instead of
        # using the structured tool_calls format. Parse and convert them.
        full_response = _try_parse_text_tool_calls(full_response)

        # Show tool calls the model is about to make
        if hasattr(full_response, "tool_calls") and full_response.tool_calls:
            for tc in full_response.tool_calls:
                name = tc.get("name", "?")
                args = tc.get("args", {})
                # run_bash already prints the command in its own confirmation prompt
                if name == "run_bash":
                    _con.print(f"  [dim]> tool:[/dim] [yellow]{name}[/yellow]")
                else:
                    args_short = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    if len(args_short) > 120:
                        args_short = args_short[:117] + "..."
                    _con.print(
                        f"  [dim]> tool:[/dim] [yellow]{name}[/yellow]"
                        f"[dim]({args_short})[/dim]"
                    )

        return {"messages": [full_response]}

    def call_tools(state: AgentState) -> AgentState:
        result = tool_node.invoke(state)
        # Show tool results (truncated for readability)
        for msg in result.get("messages", []):
            content = getattr(msg, "content", "")
            name = getattr(msg, "name", "tool")
            # run_bash output is already shown via subprocess, skip duplication
            if name == "run_bash":
                continue
            if content:
                preview = content if len(content) <= 200 else content[:197] + "..."
                _con.print(f"  [dim]< {name}:[/dim] {preview}")
        return result

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph.add_node("agent", call_llm)
    graph.add_node("tools", call_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def setup_session(cli_console: Console | None = None) -> dict:
    """
    Shared session setup for both CLI and TUI modes.
    Runs the wizard if needed, loads config, probes hardware, scans network,
    and selects the active model. Returns a dict with all session state.
    """
    _con = cli_console or Console()
    script_dir = Path(__file__).parent

    workspace_dir = resolve_workspace(script_dir)

    if not (workspace_dir / "config.md").exists():
        workspace_dir = run_wizard(workspace_dir)
    else:
        _con.print(Panel(BRIEF_SAFETY_WARNING, border_style="red"))
        run_silent_safety_check(workspace_dir, _con)

    config = load_config(workspace_dir)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "memory").mkdir(parents=True, exist_ok=True)

    hardware_summary, fit_rows = probe_hardware(workspace_dir, config.ollama_base_url)

    session_decisions, scored_remote, net_cfg = run_network_scan(
        workspace_dir, fit_rows, _con,
    )

    approved_remote = [
        s for s in scored_remote
        if session_decisions.get((s.node_label, s.model)) == "approved"
    ]

    active_model = config.default_model
    if not active_model:
        _con.print("\n[bold]Available models (from hardware probe):[/bold]")
        for r in fit_rows:
            vram = "VRAM" if r.get("fits_vram") else ("RAM" if r.get("fits_ram") else "NO FIT")
            _con.print(f"  {r['model']} ({r['size_gb']} GB) — {vram}")
        active_model = Prompt.ask(
            "\n[bold]Select model for this session[/bold]",
            default=fit_rows[0]["model"] if fit_rows else "phi4-mini",
        )

    system_prompt = _build_system_prompt(workspace_dir, hardware_summary, config, approved_remote)

    return {
        "workspace_dir": workspace_dir,
        "config": config,
        "active_model": active_model,
        "hardware_summary": hardware_summary,
        "fit_rows": fit_rows,
        "session_decisions": session_decisions,
        "scored_remote": scored_remote,
        "approved_remote": approved_remote,
        "system_prompt": system_prompt,
    }


def main():
    session = setup_session(console)
    workspace_dir = session["workspace_dir"]
    config = session["config"]
    active_model = session["active_model"]
    hardware_summary = session["hardware_summary"]
    fit_rows = session["fit_rows"]
    session_decisions = session["session_decisions"]
    scored_remote = session["scored_remote"]
    approved_remote = session["approved_remote"]

    console.print(f"\n[bold green]O.R.A. starting[/bold green] with model [cyan]{active_model}[/cyan]")
    console.print(f"[dim]  Workspace:  {workspace_dir}[/dim]")
    console.print(f"[dim]  Config:     {get_config_dir()}[/dim]")
    console.print("[dim]Type /help for commands. /settings to configure. exit to quit.[/dim]\n")

    # Mutable reference for switch_model tool to always read current active model
    active_model_ref = [active_model]

    # Build tools
    run_bash_fn = make_run_bash_tool(config, console, workspace_dir)
    switch_model_fn = make_switch_model_tool(
        workspace_dir, config.ollama_base_url, active_model_ref,
        config.require_user_confirm_switch, console,
        session_decisions=session_decisions,
        scored_remote_models=scored_remote,
    )

    @lc_tool
    def run_bash(command: str) -> str:
        """Execute a Linux shell command (requires user confirmation)."""
        return run_bash_fn(command)

    @lc_tool
    def switch_model(role: str, task_prompt: str, transfer_context: str) -> str:
        """
        Delegate a sub-task to a specialist model.
        role: reasoning | coding | fast (or any role in models.md)
        task_prompt: the specific question/task for the specialist
        transfer_context: compact context summary (<=500 tokens)
        """
        return switch_model_fn(role, task_prompt, transfer_context)

    @lc_tool
    def list_models() -> str:
        """Show current model-to-role assignments from models.md."""
        return _load_text(workspace_dir / "models.md") or "No models.md found."

    @lc_tool
    def pull_model(model_name: str) -> str:
        """Pull a model from Ollama's registry (requires user confirmation)."""
        return _pull_model(model_name, workspace_dir, config.ollama_base_url, console)

    @lc_tool
    def show_paths() -> str:
        """Show where O.R.A. stores its workspace, config, and memory files on this system."""
        config_dir = get_config_dir()
        memory_dir = workspace_dir / "memory"
        lines = [
            "O.R.A. file locations:",
            f"  Workspace (settings, profiles, models):  {workspace_dir}",
            f"  Config pointer (workspace.conf):         {config_dir}",
            f"  Memory (summaries, persistent facts):     {memory_dir}",
            "",
            "Key files:",
            f"  {workspace_dir / 'config.md'}",
            f"  {workspace_dir / 'user_profile.md'}",
            f"  {workspace_dir / 'models.md'}",
            f"  {memory_dir / 'persistent_memory.md'}",
            f"  {memory_dir / 'context_summary.md'}",
        ]
        return "\n".join(lines)

    tools = [run_bash, switch_model, list_models, pull_model, show_paths]

    # Build LLM — ChatOllama talks to Ollama's native API.
    # think=True tells Ollama to return reasoning tokens from thinking models
    # (qwen3, deepseek-r1, etc.) in a separate "thinking" field.
    llm = ChatOllama(
        model=active_model,
        base_url=config.ollama_base_url,
        temperature=0,
        think=True,
    )
    llm_with_tools = llm.bind_tools(tools)
    tool_node = ToolNode(tools)

    # Build graph
    agent_graph = build_graph(llm_with_tools, tool_node, console)

    # Session state
    messages: list = [SystemMessage(content=session["system_prompt"])]
    overflow_count = 0

    # Settings mode state
    settings_mode = False
    settings_messages: list[dict] = []
    settings_focus: str | None = None

    _write_session_state(workspace_dir, active_model, messages, overflow_count)

    # -----------------------------------------------------------------------
    # Main per-turn loop
    # -----------------------------------------------------------------------
    try:
        while True:
            try:
                user_input = Prompt.ask("[bold cyan]>[/bold cyan]")
            except (KeyboardInterrupt, EOFError):
                break

            stripped = user_input.strip()
            lower = stripped.lower()

            if lower in ("exit", "quit", "bye"):
                break

            if not stripped:
                continue

            # ---------------------------------------------------------------
            # Help
            # ---------------------------------------------------------------
            if lower == "/help":
                console.print(
                    "\n[bold]O.R.A. Commands[/bold]\n\n"
                    "  [cyan]/help[/cyan]              Show this help\n"
                    "  [cyan]/settings[/cyan]          Enter settings mode (natural language config)\n"
                    "  [cyan]/settings safety[/cyan]   Focus on safety settings\n"
                    "  [cyan]/done[/cyan]              Exit settings mode\n"
                    "  [cyan]exit[/cyan]               Save session and quit\n\n"
                    "[bold]Security settings[/bold] (in config.md)\n\n"
                    f"  bash_require_confirm:       [yellow]{config.bash_require_confirm}[/yellow]"
                    "  — require y/n before every command\n"
                    f"  bash_restrict_to_workspace: [yellow]{config.bash_restrict_to_workspace}[/yellow]"
                    "  — block commands outside workspace\n"
                    f"  bash_warn_destructive:      [yellow]{config.bash_warn_destructive}[/yellow]"
                    "  — flag dangerous commands\n\n"
                    f"  Config file: [dim]{workspace_dir / 'config.md'}[/dim]\n"
                )
                continue

            # ---------------------------------------------------------------
            # Settings mode entry
            # ---------------------------------------------------------------
            if lower.startswith("/settings"):
                settings_mode = True
                settings_messages = []
                settings_focus = _parse_settings_focus(stripped)
                focus_label = f" ({settings_focus})" if settings_focus else ""
                console.print(
                    f"\n[bold yellow][ora/settings][/bold yellow] Settings mode active{focus_label}. "
                    "I can help you configure any aspect of O.R.A.\n"
                    "  Type [bold]/done[/bold] to return to normal mode.\n"
                    "  What would you like to change?"
                )
                continue

            # ---------------------------------------------------------------
            # Settings mode exit
            # ---------------------------------------------------------------
            if settings_mode and lower in ("/done", "/exit"):
                settings_mode = False
                settings_messages = []
                settings_focus = None
                # Reload config in case settings changed
                config = load_config(workspace_dir)
                console.print("\n[bold green][ora][/bold green] Returning to normal mode.\n")
                continue

            # ---------------------------------------------------------------
            # Settings mode turn
            # ---------------------------------------------------------------
            if settings_mode:
                reply, settings_messages = _run_settings_turn(
                    user_input=stripped,
                    workspace_dir=workspace_dir,
                    active_model=active_model,
                    ollama_base_url=config.ollama_base_url,
                    settings_messages=settings_messages,
                    settings_focus=settings_focus,
                )
                console.print(f"\n[bold yellow][ora/settings][/bold yellow] {reply}\n")
                continue

            # ---------------------------------------------------------------
            # Normal agent turn
            # ---------------------------------------------------------------

            # Optionally reload config each turn
            if config.auto_reload_config:
                config = load_config(workspace_dir)

            # Vision routing — pre-process user message for file attachments
            vr = route_user_message(
                user_input, workspace_dir, config.ollama_base_url,
                active_model, console,
            )

            # If vision_handles_all strategy returned a direct response, show it
            if vr.is_direct_response:
                console.print(f"\n[bold]Ora[/bold]: {vr.message}\n")
                if config.auto_save_session_state and vr.vision_logs:
                    _write_session_state(
                        workspace_dir, active_model, messages,
                        overflow_count, vr.vision_logs,
                    )
                continue

            messages.append(HumanMessage(content=vr.message))

            # Run the agent
            try:
                result = agent_graph.invoke({"messages": messages})
                messages = result["messages"]
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")
                continue
            except Exception as exc:
                console.print(f"[red]Agent error: {exc}[/red]")
                continue

            # Response was already streamed to terminal by call_llm

            # Context management (transparent — runs after every response)
            messages, overflow_count = check_and_compact(
                messages=messages,
                active_model=active_model,
                ollama_base_url=config.ollama_base_url,
                workspace_dir=workspace_dir,
                overflow_threshold=config.overflow_threshold,
                summary_keep_last_n_turns=config.summary_keep_last_n_turns,
                max_summary_tokens=config.max_summary_tokens,
                overflow_count=overflow_count,
                console=console,
            )

            # Update session state on disk
            if config.auto_save_session_state:
                _write_session_state(
                    workspace_dir, active_model, messages,
                    overflow_count, vr.vision_logs if vr.vision_logs else None,
                )

    except KeyboardInterrupt:
        pass
    finally:
        # Save final session summary on exit
        _save_exit_summary(messages, active_model, config, workspace_dir, overflow_count)
        console.print("\n[bold]Goodbye.[/bold]")


if __name__ == "__main__":
    main()
