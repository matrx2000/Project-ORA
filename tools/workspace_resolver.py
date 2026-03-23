"""
workspace_resolver.py — Locate or create the O.R.A. workspace directory.

The workspace contains all user configuration and memory files. By default it
lives in the OS-standard user data directory (via platformdirs) to keep private
data out of any git repository.

A tiny pointer file (workspace.conf) in the OS config directory records the
chosen workspace path so it can be found on subsequent runs.
"""
import subprocess
from pathlib import Path

from platformdirs import user_data_dir, user_config_dir

APP_NAME = "ora-os"
APP_AUTHOR = "OraOS"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_default_workspace() -> Path:
    """Return the platform-default workspace path."""
    return Path(user_data_dir(APP_NAME, APP_AUTHOR))


def _get_config_dir() -> Path:
    """Return the OS config directory for the workspace pointer."""
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))


def _get_workspace_conf_path() -> Path:
    """Path to the workspace.conf pointer file."""
    return _get_config_dir() / "workspace.conf"


def read_workspace_location() -> Path | None:
    """Read the saved workspace location, or None if not configured."""
    conf = _get_workspace_conf_path()
    if not conf.exists():
        return None
    text = conf.read_text(encoding="utf-8").strip()
    if text:
        p = Path(text)
        if p.exists():
            return p
    return None


def save_workspace_location(workspace_dir: Path) -> None:
    """Save the workspace location to the config pointer file."""
    conf = _get_workspace_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(str(workspace_dir.resolve()), encoding="utf-8")


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_workspace(script_dir: Path) -> Path:
    """
    Resolve the workspace directory.  Priority:
      1. workspace.conf pointer file
      2. Legacy in-repo workspace (backward compat)
      3. Default platformdirs location
    """
    # 1. Saved pointer
    saved = read_workspace_location()
    if saved and (saved / "config.md").exists():
        return saved

    # 2. Legacy in-repo path
    legacy = script_dir / "workspace"
    if (legacy / "config.md").exists():
        return legacy

    # 3. Fresh install — return OS-standard default
    return get_default_workspace()


# ---------------------------------------------------------------------------
# Git safety
# ---------------------------------------------------------------------------

def find_git_root(path: Path) -> Path | None:
    """Walk up from *path* looking for a .git directory.  Returns repo root or None."""
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def is_gitignored(path: Path, repo_root: Path) -> bool:
    """Check whether *path* is covered by .gitignore rules in *repo_root*."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path.resolve())],
            cwd=str(repo_root),
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # git not installed or timed out — fall back to manual .gitignore scan
        return _manual_gitignore_check(path, repo_root)


def _manual_gitignore_check(path: Path, repo_root: Path) -> bool:
    """Crude fallback: check if the workspace dir name appears in .gitignore."""
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return False
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    content = gitignore.read_text(encoding="utf-8")
    # Check common patterns: "workspace/", "/workspace/", "workspace"
    name = rel.parts[0] if rel.parts else str(rel)
    for line in content.splitlines():
        line = line.strip().rstrip("/")
        if line and not line.startswith("#"):
            if line.lstrip("/") == name:
                return True
    return False


def add_to_gitignore(path: Path, repo_root: Path) -> bool:
    """
    Add *path* (relative to *repo_root*) to the repo's .gitignore.
    Returns True if the entry was added or already present.
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False  # path is not inside the repo

    pattern = f"/{rel.as_posix()}/"
    gitignore = repo_root / ".gitignore"

    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
        # Already present?
        if pattern in existing or str(rel) in existing:
            return True
        if not existing.endswith("\n"):
            existing += "\n"
        existing += (
            f"\n# O.R.A. workspace — contains private user data\n"
            f"{pattern}\n"
        )
        gitignore.write_text(existing, encoding="utf-8")
    else:
        gitignore.write_text(
            f"# O.R.A. workspace — contains private user data\n"
            f"{pattern}\n",
            encoding="utf-8",
        )
    return True


def ensure_inner_gitignore(workspace_dir: Path) -> None:
    """
    Create a .gitignore inside the workspace that ignores everything.
    Belt-and-suspenders: even if the repo-level .gitignore is missing or
    bypassed, this prevents git from tracking workspace contents.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    inner = workspace_dir / ".gitignore"
    if not inner.exists():
        inner.write_text(
            "# This directory contains private O.R.A. user data.\n"
            "# It should NEVER be committed to version control.\n"
            "*\n",
            encoding="utf-8",
        )


def check_workspace_git_safety(workspace_dir: Path, console) -> bool:
    """
    Check if *workspace_dir* is inside a git repo and handle it interactively.

    Returns True if safe to proceed, False if the user chose to abort.
    If the user chooses "pick a different path", raises WorkspaceRepick.
    """
    from rich.prompt import Prompt

    repo_root = find_git_root(workspace_dir)
    if repo_root is None:
        # Not inside a git repo — safe
        ensure_inner_gitignore(workspace_dir)
        return True

    # Inside a git repo — check if already gitignored
    if is_gitignored(workspace_dir, repo_root):
        console.print(
            "[dim]Workspace is inside a git repo but is properly gitignored. OK.[/dim]"
        )
        ensure_inner_gitignore(workspace_dir)
        return True

    # NOT gitignored — dangerous
    console.print(
        f"\n[bold red]WARNING:[/bold red] The workspace path\n"
        f"  [cyan]{workspace_dir}[/cyan]\n"
        f"is inside a git repository at\n"
        f"  [cyan]{repo_root}[/cyan]\n"
        f"and is [bold red]NOT covered by .gitignore[/bold red].\n\n"
        f"Your private data (user profile, IP addresses, memory) could be\n"
        f"accidentally committed and pushed to a remote.\n"
    )

    choice = Prompt.ask(
        "  [bold][1][/bold] Add to .gitignore automatically (recommended)\n"
        "  [bold][2][/bold] Choose a different path\n"
        "  [bold][3][/bold] Abort setup\n\n"
        "  Choice",
        choices=["1", "2", "3"],
        default="1",
    )

    if choice == "1":
        added = add_to_gitignore(workspace_dir, repo_root)
        ensure_inner_gitignore(workspace_dir)
        if added:
            # Verify the fix
            if is_gitignored(workspace_dir, repo_root):
                console.print("[green]Added to .gitignore and verified.[/green]")
                return True
            else:
                # Verification failed but we did add the entry — inner gitignore
                # is our fallback
                console.print(
                    "[yellow]Added to .gitignore (git verify inconclusive, "
                    "but inner .gitignore is in place as safety net).[/yellow]"
                )
                return True
        console.print("[red]Failed to update .gitignore.[/red]")
        return False

    if choice == "2":
        raise WorkspaceRepick()

    # choice == "3"
    return False


class WorkspaceRepick(Exception):
    """Raised when the user wants to pick a different workspace path."""


def run_silent_safety_check(workspace_dir: Path, console) -> None:
    """
    Non-interactive safety check for subsequent boots.
    Prints a warning if the workspace is inside an un-ignored git repo.
    Always ensures the inner .gitignore exists.
    """
    ensure_inner_gitignore(workspace_dir)

    repo_root = find_git_root(workspace_dir)
    if repo_root is None:
        return

    if not is_gitignored(workspace_dir, repo_root):
        console.print(
            "[bold red][ora/safety][/bold red] Workspace is inside a git repo "
            "and NOT gitignored. Run [bold]/settings[/bold] to fix."
        )
