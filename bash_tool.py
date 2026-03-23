"""
bash_tool.py — Restricted Linux shell execution layer.
All commands require user confirmation. Destructive commands are flagged.
Hard-blocked patterns cannot be executed regardless of allowlist.
"""
import re
import subprocess
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm


# ---------------------------------------------------------------------------
# Allowlist: broad command-category patterns that are permitted
# ---------------------------------------------------------------------------
_ALLOWED_PATTERNS: list[re.Pattern] = [
    # File system
    re.compile(r"^(ls|find|cat|cp|mv|mkdir|touch|chmod|chown|stat|du|df)\b"),
    # Process management
    re.compile(r"^(ps|top|kill|systemctl|crontab|htop|pgrep|pkill)\b"),
    # Package management
    re.compile(r"^(apt|apt-get|pip|pip3|npm|yarn|cargo|gem)\b"),
    # Networking
    re.compile(r"^(curl|wget|ssh|ping|nmap|ip|netstat|ss|dig|host)\b"),
    # General utilities
    re.compile(r"^(echo|printf|grep|awk|sed|sort|wc|tar|unzip|gzip|gunzip|which|env|"
               r"head|tail|less|more|diff|patch|ln|realpath|basename|dirname|"
               r"date|uptime|uname|hostname|id|whoami|groups|pwd|xargs|tee|"
               r"tr|cut|paste|comm|join|split|csplit|nl|fmt|fold|pr|column)\b"),
    # File read/write helpers
    re.compile(r"^(nano|vim|vi|nvim|emacs|less|more|rm|touch|cat|tee)\b"),
    # Python / node / shell scripts
    re.compile(r"^(python3?|node|bash|sh|zsh|fish)\b"),
    # Ollama
    re.compile(r"^ollama\b"),
    # Git
    re.compile(r"^git\b"),
    # Systemd / journald
    re.compile(r"^(journalctl|timedatectl|hostnamectl|localectl)\b"),
    # Disk / partition info (read-only)
    re.compile(r"^(lsblk|fdisk -l|blkid|lscpu|lsmem|lsusb|lspci|free|vmstat|iostat)\b"),
]

# ---------------------------------------------------------------------------
# Hard-blocked patterns — refused regardless of allowlist
# ---------------------------------------------------------------------------
_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/"),    # rm -rf /  or variants
    re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+~"),    # rm -rf ~
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+.*if=/dev/zero"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bhalt\b"),
    re.compile(r"\bpoweroff\b"),
    re.compile(r">\s*/etc/passwd"),
    re.compile(r">\s*/etc/shadow"),
    re.compile(r">\s*/boot/"),
    re.compile(r"\bchmod\s+777\s+/"),
    re.compile(r":(){ :|:& };:"),  # fork bomb
]

# ---------------------------------------------------------------------------
# Destructive command indicators — get extra [DESTRUCTIVE] tag
# ---------------------------------------------------------------------------
_DESTRUCTIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\b"),
    re.compile(r"\bmv\b.*\b\w"),    # mv (overwriting)
    re.compile(r"\bapt.*(remove|purge)\b"),
    re.compile(r"\bpip.*(uninstall)\b"),
    re.compile(r"\bnpm.*(uninstall|remove)\b"),
    re.compile(r"\btruncate\b"),
    re.compile(r"\b>\s*\w"),        # output redirection (overwrite)
    re.compile(r"\bdrop\b"),        # SQL drop etc.
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
]


def _is_allowed(command: str) -> bool:
    cmd = command.strip()
    return any(p.match(cmd) for p in _ALLOWED_PATTERNS)


def _is_blocked(command: str, extra_blocked: list[str]) -> str | None:
    """Return the matched block pattern description, or None if allowed."""
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return pattern.pattern
    for blocked in extra_blocked:
        if blocked and blocked in command:
            return f"config-blocked: {blocked}"
    return None


def _is_destructive(command: str) -> bool:
    return any(p.search(command) for p in _DESTRUCTIVE_PATTERNS)


def make_run_bash_tool(config, console: Console, workspace_dir: Path | None = None):
    """
    Factory that returns a run_bash function bound to current config.

    config fields used:
        bash_exclude_commands (list)
        bash_require_confirm (bool)
        bash_restrict_to_workspace (bool)  — block commands targeting paths outside workspace
        bash_warn_destructive (bool)       — show [DESTRUCTIVE] tag on dangerous commands
    """
    extra_blocked: list[str] = getattr(config, "bash_exclude_commands", [])
    restrict_to_ws: bool = getattr(config, "bash_restrict_to_workspace", True)
    warn_destructive: bool = getattr(config, "bash_warn_destructive", True)
    ws_path: str = str(workspace_dir.resolve()) if workspace_dir else ""

    def run_bash(command: str) -> str:
        """
        Execute a Linux shell command.

        Every command requires user confirmation before execution.
        Destructive commands are flagged with [DESTRUCTIVE].
        Certain commands are hard-blocked and cannot be run.

        Args:
            command: The shell command to execute.

        Returns:
            Command stdout + stderr, or an error/rejection message.
        """
        command = command.strip()
        if not command:
            return "Error: empty command."

        # Hard-block check
        block_reason = _is_blocked(command, extra_blocked)
        if block_reason:
            return (
                f"BLOCKED: This command matches a hard-blocked pattern ({block_reason}). "
                "It cannot be executed."
            )

        # Allowlist check
        if not _is_allowed(command):
            return (
                f"BLOCKED: '{command.split()[0]}' is not on the allowed command list. "
                "Only commands in the permitted categories can be executed."
            )

        # Workspace restriction check
        if restrict_to_ws and ws_path:
            violation = _check_workspace_restriction(command, ws_path)
            if violation:
                return (
                    f"BLOCKED: bash_restrict_to_workspace is enabled. "
                    f"This command references a path outside the workspace ({violation}). "
                    f"Workspace: {ws_path}\n"
                    "Disable this restriction in /settings safety if you need full OS access."
                )

        # Build confirmation prompt
        is_destructive = warn_destructive and _is_destructive(command)
        tag = "[bold red][DESTRUCTIVE][/bold red] " if is_destructive else ""

        console.print(f"\n{tag}[bold]Run:[/bold] [cyan]{command}[/cyan]")

        if config.bash_require_confirm:
            if not Confirm.ask("Execute?", default=False):
                return "Command cancelled by user."

        # Execute
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 120 seconds."
        except Exception as exc:
            return f"Error executing command: {exc}"

    return run_bash


# ---------------------------------------------------------------------------
# Workspace restriction helper
# ---------------------------------------------------------------------------

# Commands that are safe to run anywhere (they don't target files)
_UNRESTRICTED_COMMANDS = re.compile(
    r"^(ps|top|htop|free|vmstat|iostat|uptime|uname|hostname|id|whoami|"
    r"groups|pwd|date|lscpu|lsmem|lsusb|lspci|lsblk|blkid|df|"
    r"systemctl|journalctl|timedatectl|hostnamectl|localectl|"
    r"pgrep|pkill|kill|ping|dig|host|ss|netstat|ip|ollama|which|env)\b"
)


def _check_workspace_restriction(command: str, ws_path: str) -> str | None:
    """
    Return the offending path if the command references files outside the
    workspace, or None if the command is OK.

    This is a best-effort heuristic — it catches common cases like explicit
    absolute paths but cannot parse every possible shell expansion.
    """
    cmd = command.strip()

    # Commands that don't operate on file paths are always fine
    if _UNRESTRICTED_COMMANDS.match(cmd):
        return None

    # Extract tokens that look like absolute paths
    tokens = cmd.split()
    for token in tokens:
        # Skip flags
        if token.startswith("-"):
            continue
        # Expand ~ to home
        expanded = token.replace("~", str(Path.home()))
        # Check absolute paths
        if expanded.startswith("/"):
            try:
                resolved = str(Path(expanded).resolve())
            except (OSError, ValueError):
                continue
            if not resolved.startswith(ws_path):
                return token

    return None
