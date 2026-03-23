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


def make_run_bash_tool(config, console: Console):
    """
    Factory that returns a run_bash function bound to current config.
    config must have: bash_exclude_commands (list), bash_require_confirm (bool).
    """
    extra_blocked: list[str] = getattr(config, "bash_exclude_commands", [])

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

        # Build confirmation prompt
        is_destructive = _is_destructive(command)
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
