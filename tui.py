"""
tui.py — O.R.A. Terminal User Interface (Textual)
Three-panel layout: Thinking & Tools | Conversation | Settings (file editor)
"""
import sys
import asyncio
import threading
from pathlib import Path
from typing import Annotated

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, Static, Input, Button,
    DirectoryTree, TextArea, RichLog,
)
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.reactive import reactive
from textual import work, on

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool as lc_tool
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from main import (
    OraConfig, load_config, _build_system_prompt, _load_text,
    _write_session_state, _try_parse_text_tool_calls, setup_session,
)
from bash_tool import make_run_bash_tool
from tools.model_switcher import make_switch_model_tool
from tools.ollama_manager import list_models as _list_models, pull_model as _pull_model
from tools.context_manager import check_and_compact
from tools.vision_router import route_user_message
from tools.workspace_resolver import get_config_dir


# ---------------------------------------------------------------------------
# Bash confirmation modal
# ---------------------------------------------------------------------------

class ConfirmScreen(ModalScreen[bool]):
    """Modal dialog for confirming bash commands."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "deny", "No"),
        Binding("escape", "deny", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 70;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    """

    def __init__(self, command: str, is_destructive: bool = False):
        super().__init__()
        self.command = command
        self.is_destructive = is_destructive

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            if self.is_destructive:
                yield Static("[bold red]DESTRUCTIVE COMMAND[/bold red]")
            yield Static(f"\n[bold]Execute:[/bold]\n[cyan]{self.command}[/cyan]\n")
            yield Static(
                "[dim]Press [bold]y[/bold] to confirm, "
                "[bold]n[/bold] or [bold]Esc[/bold] to cancel[/dim]"
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Main TUI Application
# ---------------------------------------------------------------------------

class OraApp(App):
    """O.R.A. — Orchestrated Reasoning Agent — Terminal UI"""

    TITLE = "O.R.A."
    SUB_TITLE = "Orchestrated Reasoning Agent"

    CSS = """
    Screen {
        background: $surface;
    }

    #app-layout {
        height: 1fr;
    }

    /* ---- Left panel: thinking & tools ---- */

    #left-panel {
        width: 30;
        min-width: 22;
        background: $surface-darken-1;
        border-right: vkey $primary-background-darken-2;
    }

    #thinking-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        padding: 0 1;
    }

    /* ---- Center panel: conversation ---- */

    #center-panel {
        width: 1fr;
        min-width: 40;
    }

    #chat-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        padding: 0 1;
    }

    #user-input {
        dock: bottom;
        margin: 0 1;
    }

    /* ---- Right panel: settings / file editor ---- */

    #right-panel {
        width: 38;
        min-width: 28;
        background: $surface-darken-1;
        border-left: vkey $primary-background-darken-2;
    }

    #file-tree {
        height: 2fr;
        scrollbar-size: 1 1;
    }

    #file-path-label {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text-muted;
    }

    #file-editor {
        height: 3fr;
    }

    #editor-bar {
        dock: bottom;
        height: 3;
        align: center middle;
        padding: 0 1;
    }

    /* ---- Shared ---- */

    .panel-header {
        width: 100%;
        height: 1;
        background: $primary-darken-3;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }

    .panel-header-settings {
        width: 100%;
        height: 1;
        background: $warning-darken-2;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }

    #status-bar {
        dock: bottom;
        width: 100%;
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 2;
    }

    .msg-user {
        margin: 1 0 0 0;
    }

    .msg-ora {
        margin: 0 0 0 0;
    }

    .msg-system {
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save_file", "Save File", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    def __init__(self, session: dict, **kwargs):
        super().__init__(**kwargs)
        self.workspace_dir: Path = session["workspace_dir"]
        self.config: OraConfig = session["config"]
        self.active_model: str = session["active_model"]
        self.hardware_summary: str = session["hardware_summary"]
        self.session_decisions = session["session_decisions"]
        self.scored_remote = session["scored_remote"]
        self.approved_remote = session["approved_remote"]
        self.system_prompt: str = session["system_prompt"]

        self.active_model_ref = [self.active_model]
        self.messages: list = [SystemMessage(content=self.system_prompt)]
        self.overflow_count = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_buffer = ""
        self._think_buffer = ""
        self._streaming_widget: Static | None = None
        self._thinking_widget: Static | None = None
        self._current_file = ""
        self._settings_open = False

        # Agent graph built in on_mount (needs self for callbacks)
        self.agent_graph = None

    # -------------------------------------------------------------------
    # Layout
    # -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-layout"):
            # LEFT: thinking & tool calls
            with Vertical(id="left-panel"):
                yield Static(" Thinking & Tools", classes="panel-header")
                with ScrollableContainer(id="thinking-scroll"):
                    pass

            # CENTER: conversation
            with Vertical(id="center-panel"):
                yield Static(" Conversation", classes="panel-header")
                with ScrollableContainer(id="chat-scroll"):
                    yield Static(
                        f"[bold green]O.R.A.[/bold green] ready — model: "
                        f"[cyan]{self.active_model}[/cyan]\n"
                        f"[dim]Type /settings to open settings. Ctrl+Q to quit.[/dim]",
                        classes="msg-system",
                    )
                yield Input(
                    placeholder="Type a message... (/settings to configure)",
                    id="user-input",
                )

            # RIGHT: settings file editor (hidden by default)
            with Vertical(id="right-panel"):
                yield Static(" Settings", classes="panel-header-settings")
                yield DirectoryTree(str(self.workspace_dir), id="file-tree")
                yield Static("No file selected", id="file-path-label")
                yield TextArea(id="file-editor", language="markdown")
                with Horizontal(id="editor-bar"):
                    yield Button("Save", id="save-btn", variant="success")
                    yield Button("Close", id="close-btn", variant="error")

        yield Static(
            f" Model: {self.active_model} | "
            f"Workspace: {self.workspace_dir} | "
            f"Config: {get_config_dir()}",
            id="status-bar",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Hide settings panel
        self.query_one("#right-panel").display = False
        # Build agent (needs self for callbacks)
        self._build_agent()
        # Focus input
        self.query_one("#user-input", Input).focus()

    # -------------------------------------------------------------------
    # Agent setup (runs once in on_mount)
    # -------------------------------------------------------------------

    def _build_agent(self) -> None:
        """Create tools, LLM, and LangGraph agent."""
        config = self.config
        workspace_dir = self.workspace_dir

        run_bash_fn = make_run_bash_tool(
            config, workspace_dir=workspace_dir,
            confirm_callback=self._request_confirm,
        )
        switch_model_fn = make_switch_model_tool(
            workspace_dir, config.ollama_base_url, self.active_model_ref,
            config.require_user_confirm_switch, None,
            session_decisions=self.session_decisions,
            scored_remote_models=self.scored_remote,
        )

        @lc_tool
        def run_bash(command: str) -> str:
            """Execute a Linux shell command (requires user confirmation)."""
            return run_bash_fn(command)

        @lc_tool
        def switch_model(role: str, task_prompt: str, transfer_context: str) -> str:
            """Delegate a sub-task to a specialist model."""
            return switch_model_fn(role, task_prompt, transfer_context)

        @lc_tool
        def list_models() -> str:
            """Show viable_models.md with live hardware fit scores."""
            return _list_models(workspace_dir, None, config.ollama_base_url)

        @lc_tool
        def pull_model(model_name: str) -> str:
            """Pull a model from Ollama's registry."""
            return _pull_model(model_name, workspace_dir, config.ollama_base_url, None)

        @lc_tool
        def show_paths() -> str:
            """Show where O.R.A. stores its files on this system."""
            memory_dir = workspace_dir / "memory"
            lines = [
                "O.R.A. file locations:",
                f"  Workspace:  {workspace_dir}",
                f"  Config:     {get_config_dir()}",
                f"  Memory:     {memory_dir}",
            ]
            return "\n".join(lines)

        tools = [run_bash, switch_model, list_models, pull_model, show_paths]

        llm = ChatOllama(
            model=self.active_model,
            base_url=config.ollama_base_url,
            temperature=0,
            think=True,
        )
        llm_with_tools = llm.bind_tools(tools)
        tool_node = ToolNode(tools)

        self.agent_graph = self._build_graph(llm_with_tools, tool_node)

    def _build_graph(self, llm_with_tools, tool_node):
        """Build LangGraph ReAct graph with TUI streaming callbacks."""
        app = self

        class AgentState(dict):
            messages: Annotated[list, add_messages]

        graph = StateGraph(AgentState)

        def call_llm(state):
            full_response = None
            in_think = False
            has_content = False

            for chunk in llm_with_tools.stream(state["messages"]):
                if full_response is None:
                    full_response = chunk
                else:
                    full_response = full_response + chunk

                # Check for thinking tokens
                thinking = ""
                if hasattr(chunk, "thinking") and chunk.thinking:
                    thinking = chunk.thinking
                elif hasattr(chunk, "additional_kwargs") and chunk.additional_kwargs:
                    thinking = chunk.additional_kwargs.get("thinking", "")

                if thinking:
                    if not in_think:
                        in_think = True
                        app.call_from_thread(app._ui_start_thinking)
                    app.call_from_thread(app._ui_append_thinking, thinking)
                elif chunk.content:
                    if in_think:
                        in_think = False
                        app.call_from_thread(app._ui_finish_thinking)
                    if not has_content:
                        has_content = True
                        app.call_from_thread(app._ui_start_response)
                    app.call_from_thread(app._ui_append_response, chunk.content)

            if in_think:
                app.call_from_thread(app._ui_finish_thinking)
            if has_content:
                app.call_from_thread(app._ui_finish_response)

            full_response = _try_parse_text_tool_calls(full_response)

            if hasattr(full_response, "tool_calls") and full_response.tool_calls:
                for tc in full_response.tool_calls:
                    app.call_from_thread(
                        app._ui_show_tool_call,
                        tc.get("name", "?"),
                        tc.get("args", {}),
                    )

            return {"messages": [full_response]}

        def call_tools(state):
            result = tool_node.invoke(state)
            for msg in result.get("messages", []):
                content = getattr(msg, "content", "")
                name = getattr(msg, "name", "tool")
                if name != "run_bash" and content:
                    app.call_from_thread(app._ui_show_tool_result, name, content)
            return result

        def route(state):
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

    # -------------------------------------------------------------------
    # UI update methods (called from main thread via call_from_thread)
    # -------------------------------------------------------------------

    def _ui_add_user_message(self, text: str) -> None:
        scroll = self.query_one("#chat-scroll")
        scroll.mount(Static(f"[bold cyan]>[/bold cyan] {text}", classes="msg-user"))
        scroll.scroll_end(animate=False)

    def _ui_start_response(self) -> None:
        scroll = self.query_one("#chat-scroll")
        self._stream_buffer = ""
        self._streaming_widget = Static("[bold]Ora[/bold]: ", classes="msg-ora")
        scroll.mount(self._streaming_widget)

    def _ui_append_response(self, token: str) -> None:
        self._stream_buffer += token
        if self._streaming_widget:
            self._streaming_widget.update(
                f"[bold]Ora[/bold]: {self._stream_buffer}"
            )
            self.query_one("#chat-scroll").scroll_end(animate=False)

    def _ui_finish_response(self) -> None:
        self._streaming_widget = None

    def _ui_add_system_message(self, text: str) -> None:
        scroll = self.query_one("#chat-scroll")
        scroll.mount(Static(f"[dim]{text}[/dim]", classes="msg-system"))
        scroll.scroll_end(animate=False)

    # ---- Thinking panel ----

    def _ui_start_thinking(self) -> None:
        scroll = self.query_one("#thinking-scroll")
        self._think_buffer = ""
        self._thinking_widget = Static(
            "[bold magenta]thinking ...[/bold magenta]",
        )
        scroll.mount(self._thinking_widget)

    def _ui_append_thinking(self, token: str) -> None:
        self._think_buffer += token
        if self._thinking_widget:
            self._thinking_widget.update(
                "[bold magenta]thinking ...[/bold magenta]\n"
                f"[dim italic magenta]{self._think_buffer}[/dim italic magenta]"
            )
            self.query_one("#thinking-scroll").scroll_end(animate=False)

    def _ui_finish_thinking(self) -> None:
        if self._thinking_widget and self._think_buffer:
            self._thinking_widget.update(
                "[magenta]──── thought ────[/magenta]\n"
                f"[dim italic magenta]{self._think_buffer}[/dim italic magenta]\n"
                "[magenta]────────────────[/magenta]"
            )
        self._thinking_widget = None
        self._think_buffer = ""

    def _ui_show_tool_call(self, name: str, args: dict) -> None:
        scroll = self.query_one("#thinking-scroll")
        if name == "run_bash":
            cmd = args.get("command", "")
            text = f"[yellow]> [bold]{name}[/bold][/yellow]\n[dim]{cmd}[/dim]"
        else:
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            if len(args_str) > 80:
                args_str = args_str[:77] + "..."
            text = f"[yellow]> [bold]{name}[/bold][/yellow]\n[dim]{args_str}[/dim]"
        scroll.mount(Static(text))
        scroll.scroll_end(animate=False)

    def _ui_show_tool_result(self, name: str, content: str) -> None:
        scroll = self.query_one("#thinking-scroll")
        preview = content if len(content) <= 200 else content[:197] + "..."
        scroll.mount(Static(f"[dim]< {name}: {preview}[/dim]"))
        scroll.scroll_end(animate=False)

    # -------------------------------------------------------------------
    # Bash confirmation (called from worker thread)
    # -------------------------------------------------------------------

    def _request_confirm(self, command: str, is_destructive: bool) -> bool:
        """Called from worker thread. Pushes modal and blocks until answered."""
        future = asyncio.run_coroutine_threadsafe(
            self.push_screen_wait(ConfirmScreen(command, is_destructive)),
            self._loop,
        )
        return future.result(timeout=300)

    # -------------------------------------------------------------------
    # Settings panel
    # -------------------------------------------------------------------

    def _open_settings(self) -> None:
        self._settings_open = True
        self.query_one("#right-panel").display = True
        # Refresh the directory tree
        tree = self.query_one("#file-tree", DirectoryTree)
        tree.reload()

    def _close_settings(self) -> None:
        self._settings_open = False
        self.query_one("#right-panel").display = False
        # Reload config in case files were edited
        self.config = load_config(self.workspace_dir)
        self.query_one("#user-input", Input).focus()

    @on(DirectoryTree.FileSelected, "#file-tree")
    def on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = event.path
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            self.notify(f"Cannot read: {e}", severity="error")
            return
        self._current_file = str(path)
        self.query_one("#file-path-label", Static).update(f" {path.name}")
        self.query_one("#file-editor", TextArea).load_text(content)

    @on(Button.Pressed, "#save-btn")
    def on_save_pressed(self) -> None:
        self.action_save_file()

    @on(Button.Pressed, "#close-btn")
    def on_close_pressed(self) -> None:
        self._close_settings()
        self._ui_add_system_message("Settings closed. Config reloaded.")

    def action_save_file(self) -> None:
        if not self._current_file:
            self.notify("No file open", severity="warning")
            return
        editor = self.query_one("#file-editor", TextArea)
        try:
            Path(self._current_file).write_text(editor.text, encoding="utf-8")
            self.notify(f"Saved: {Path(self._current_file).name}")
            if Path(self._current_file).name == "config.md":
                self.config = load_config(self.workspace_dir)
                self.notify("Config reloaded")
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error")

    # -------------------------------------------------------------------
    # Input handling
    # -------------------------------------------------------------------

    @on(Input.Submitted, "#user-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        lower = text.lower()

        if lower in ("exit", "quit", "bye"):
            self._save_and_exit()
            return

        if lower.startswith("/settings"):
            self._open_settings()
            self._ui_add_system_message("Settings panel opened. Select a file to edit.")
            return

        if lower in ("/done", "/close"):
            self._close_settings()
            self._ui_add_system_message("Settings closed. Config reloaded.")
            return

        # Disable input while agent works
        event.input.disabled = True
        self._ui_add_user_message(text)
        self._run_agent_turn(text)

    # -------------------------------------------------------------------
    # Agent worker
    # -------------------------------------------------------------------

    @work(exclusive=True, thread=True)
    def _run_agent_turn(self, user_input: str) -> None:
        """Run one full agent turn in a background thread."""
        # Vision routing
        vr = route_user_message(
            user_input, self.workspace_dir, self.config.ollama_base_url,
            self.active_model, None,
        )

        if vr.is_direct_response:
            self.call_from_thread(self._ui_start_response)
            self.call_from_thread(self._ui_append_response, vr.message)
            self.call_from_thread(self._ui_finish_response)
            self.call_from_thread(self._enable_input)
            return

        self.messages.append(HumanMessage(content=vr.message))

        try:
            result = self.agent_graph.invoke({"messages": self.messages})
            self.messages = result["messages"]
        except Exception as exc:
            self.call_from_thread(
                self._ui_add_system_message, f"Agent error: {exc}"
            )
            self.call_from_thread(self._enable_input)
            return

        # Context management
        self.messages, self.overflow_count = check_and_compact(
            messages=self.messages,
            active_model=self.active_model,
            ollama_base_url=self.config.ollama_base_url,
            workspace_dir=self.workspace_dir,
            overflow_threshold=self.config.overflow_threshold,
            summary_keep_last_n_turns=self.config.summary_keep_last_n_turns,
            max_summary_tokens=self.config.max_summary_tokens,
            overflow_count=self.overflow_count,
            console=None,
        )

        # Save session state
        if self.config.auto_save_session_state:
            _write_session_state(
                self.workspace_dir, self.active_model, self.messages,
                self.overflow_count,
            )

        self.call_from_thread(self._enable_input)

    def _enable_input(self) -> None:
        inp = self.query_one("#user-input", Input)
        inp.disabled = False
        inp.focus()

    # -------------------------------------------------------------------
    # Exit
    # -------------------------------------------------------------------

    def _save_and_exit(self) -> None:
        _write_session_state(
            self.workspace_dir, self.active_model, self.messages,
            self.overflow_count,
        )
        self.exit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    from rich.console import Console

    cli = Console()
    cli.print("[bold]O.R.A.[/bold] — setting up...\n")

    # CLI-based setup (wizard, model selection, etc.) before TUI launches
    session = setup_session(cli)

    cli.print(
        f"\n[bold green]Launching TUI[/bold green] with model "
        f"[cyan]{session['active_model']}[/cyan]...\n"
    )

    app = OraApp(session)
    app.run()


if __name__ == "__main__":
    main()
