"""
network_scanner.py — Discover, score, and approve remote Ollama nodes.

Flow:
1. parse_network_config()  — read workspace/network_config.md
2. scan_nodes()            — async HTTP probe all nodes, inventory models
3. build_registry()        — cross-reference trust, score vs local, write network_registry.md
4. run_startup_approval()  — show recommendations, ask user, return session_approved set
"""
import asyncio
import datetime
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RemoteNode:
    label: str
    address: str          # "192.168.1.42:11434"
    notes: str = ""
    online: bool = False
    latency_ms: int = 0
    models: list[dict] = field(default_factory=list)  # [{name, size_gb}]


@dataclass
class RemoteModelDesc:
    node: str
    model: str
    role: str
    description: str


@dataclass
class NetworkConfig:
    nodes: list[RemoteNode] = field(default_factory=list)
    remote_descriptions: list[RemoteModelDesc] = field(default_factory=list)
    scan_timeout: int = 3
    notify_on_discovery: bool = True
    local_priority: bool = True
    remote_preference_threshold: float = 0.20
    require_approval_for_new_models: bool = True
    remember_approvals: bool = True


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_table(content: str, header_keyword: str) -> list[list[str]]:
    """Extract rows from the first markdown table whose header contains header_keyword."""
    rows = []
    in_table = False
    past_separator = False
    for line in content.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("|") and header_keyword.lower() in stripped.lower():
                in_table = True
            continue
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            past_separator = True
            continue
        if past_separator and stripped.startswith("|"):
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            rows.append(parts)
        elif past_separator and not stripped.startswith("|"):
            break
    return rows


def parse_network_config(workspace_dir: Path) -> NetworkConfig:
    cfg = NetworkConfig()
    path = workspace_dir / "network_config.md"
    if not path.exists():
        return cfg

    content = path.read_text(encoding="utf-8")

    # Nodes table
    for row in _parse_table(content, "label"):
        if len(row) >= 2:
            cfg.nodes.append(RemoteNode(
                label=row[0],
                address=row[1],
                notes=row[2] if len(row) > 2 else "",
            ))

    # Remote model descriptions table
    for row in _parse_table(content, "node"):
        if len(row) >= 4:
            cfg.remote_descriptions.append(RemoteModelDesc(
                node=row[0],
                model=row[1],
                role=row[2],
                description=row[3],
            ))

    # Scalar settings
    for line in content.splitlines():
        stripped = line.strip()
        if ": " in stripped and not stripped.startswith("|") and not stripped.startswith("#"):
            key, value = stripped.split(": ", 1)
            key, value = key.strip(), value.strip()
            # Strip inline comments
            if "#" in value:
                value = value.split("#")[0].strip()
            if key == "scan_timeout_seconds":
                try:
                    cfg.scan_timeout = int(value)
                except ValueError:
                    pass
            elif key == "notify_on_discovery":
                cfg.notify_on_discovery = value.lower() == "true"
            elif key == "local_priority":
                cfg.local_priority = value.lower() == "true"
            elif key == "remote_preference_threshold":
                try:
                    cfg.remote_preference_threshold = float(value)
                except ValueError:
                    pass
            elif key == "require_approval_for_new_models":
                cfg.require_approval_for_new_models = value.lower() == "true"
            elif key == "remember_approvals":
                cfg.remember_approvals = value.lower() == "true"

    return cfg


def parse_trust_registry(workspace_dir: Path) -> dict[tuple[str, str], str]:
    """
    Return {(node_label, model_name): "approved"|"declined"|"revoked"}.
    """
    trust: dict[tuple[str, str], str] = {}
    path = workspace_dir / "network_trust.md"
    if not path.exists():
        return trust

    content = path.read_text(encoding="utf-8")
    in_approved = False
    in_revoked = False
    past_sep = False

    for line in content.splitlines():
        stripped = line.strip()
        if "## Remembered approvals" in stripped:
            in_approved = True
            in_revoked = False
            past_sep = False
            continue
        if "## Permanently revoked" in stripped:
            in_approved = False
            in_revoked = True
            past_sep = False
            continue
        if stripped.startswith("#"):
            in_approved = False
            in_revoked = False
            continue
        if stripped.startswith("|---"):
            past_sep = True
            continue
        if past_sep and stripped.startswith("|"):
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if in_approved and len(parts) >= 3:
                trust[(parts[0], parts[1])] = parts[2]  # "approved" or "declined"
            elif in_revoked and len(parts) >= 2:
                trust[(parts[0], parts[1])] = "revoked"

    return trust


# ---------------------------------------------------------------------------
# Node scanner
# ---------------------------------------------------------------------------

async def _probe_node(node: RemoteNode, timeout: int) -> RemoteNode:
    """Probe a single Ollama node. Mutates and returns the node."""
    url = f"http://{node.address}/api/tags"
    try:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        node.latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            node.online = True
            node.models = [
                {
                    "name": m["name"],
                    "size_gb": round(m.get("size", 0) / 1024 ** 3, 1),
                }
                for m in data.get("models", [])
            ]
    except Exception:
        node.online = False
    return node


async def _scan_all(nodes: list[RemoteNode], timeout: int) -> list[RemoteNode]:
    return await asyncio.gather(*[_probe_node(n, timeout) for n in nodes])


def scan_nodes(net_cfg: NetworkConfig) -> list[RemoteNode]:
    """Synchronously scan all nodes. Returns updated node list."""
    if not net_cfg.nodes:
        return []
    return asyncio.run(_scan_all(net_cfg.nodes, net_cfg.scan_timeout))


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------

def _local_model_size(name: str, local_fit_rows: list[dict]) -> float:
    """Return size_gb of a local model by name, or 0 if not found."""
    for r in local_fit_rows:
        if r["model"] == name:
            return r["size_gb"]
    return 0.0


def _local_model_for_role(role: str, local_fit_rows: list[dict]) -> dict | None:
    """Return the first local model matching a role, or None."""
    for r in local_fit_rows:
        if r.get("role", "") == role:
            return r
    return None


@dataclass
class ScoredRemoteModel:
    node_label: str
    node_address: str
    model: str
    size_gb: float
    role: str
    description: str
    trust: str          # "approved", "declined", "revoked", "pending"
    vs_local: str       # human-readable comparison string
    is_better: bool     # exceeds local by preference_threshold


def build_registry(
    net_cfg: NetworkConfig,
    nodes: list[RemoteNode],
    local_fit_rows: list[dict],
    trust: dict[tuple[str, str], str],
    workspace_dir: Path,
) -> list[ScoredRemoteModel]:
    """
    Score remote models vs local, write network_registry.md.
    Returns list of ScoredRemoteModel (excluding revoked).
    """
    # Build description lookup: (node_label, model) -> RemoteModelDesc
    desc_map: dict[tuple[str, str], RemoteModelDesc] = {}
    for d in net_cfg.remote_descriptions:
        desc_map[(d.node, d.model)] = d

    scored: list[ScoredRemoteModel] = []
    recommendations: list[ScoredRemoteModel] = []

    for node in nodes:
        if not node.online:
            continue
        for m in node.models:
            key = (node.label, m["name"])
            trust_state = trust.get(key, "pending")
            if trust_state == "revoked":
                continue

            desc = desc_map.get(key)
            role = desc.role if desc else "general"
            description = desc.description if desc else ""

            # Score vs local
            local_match = _local_model_for_role(role, local_fit_rows)
            if local_match:
                local_size = local_match["size_gb"]
                if local_size > 0:
                    ratio = (m["size_gb"] - local_size) / local_size
                else:
                    ratio = 1.0
                is_better = ratio >= net_cfg.remote_preference_threshold
                if m["size_gb"] > local_size:
                    vs = f"larger ({m['size_gb']}GB vs {local_size}GB local)"
                elif m["size_gb"] == local_size:
                    vs = "same size as local"
                else:
                    vs = f"smaller than local ({local_size}GB)"
            else:
                is_better = True
                vs = "no local equivalent"

            sm = ScoredRemoteModel(
                node_label=node.label,
                node_address=node.address,
                model=m["name"],
                size_gb=m["size_gb"],
                role=role,
                description=description,
                trust=trust_state,
                vs_local=vs,
                is_better=is_better,
            )
            scored.append(sm)
            if is_better and trust_state != "declined":
                recommendations.append(sm)

    # Write network_registry.md
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# Network Registry\n_Scanned: {now}_\n"]

    # Local models section
    lines.append("## Local models (priority)")
    lines.append("| model | size_gb | role |")
    lines.append("|-------|---------|------|")
    for r in local_fit_rows:
        lines.append(f"| {r['model']:<22} | {r['size_gb']:<7} | {r.get('role', '')} |")
    lines.append("")

    # Remote nodes
    lines.append("## Remote nodes\n")
    for node in nodes:
        status = f"online — {node.latency_ms}ms" if node.online else "offline"
        sym = "online" if node.online else "offline"
        lines.append(f"### {node.label} ({node.address}) [{sym}] — {status}")
        if node.online:
            lines.append("| model | size_gb | role | vs local | trust |")
            lines.append("|-------|---------|------|----------|-------|")
            node_scored = [s for s in scored if s.node_label == node.label]
            for s in node_scored:
                lines.append(
                    f"| {s.model:<22} | {s.size_gb:<7} | {s.role:<10} | {s.vs_local:<22} | {s.trust} |"
                )
        lines.append("")

    if recommendations:
        lines.append("## Startup recommendations")
        for r in recommendations:
            lines.append(
                f"Better remote option for role '{r.role}': "
                f"{r.model} on {r.node_label} ({r.size_gb}GB) — {r.vs_local}"
            )

    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "network_registry.md").write_text("\n".join(lines), encoding="utf-8")

    return scored


# ---------------------------------------------------------------------------
# Trust writer
# ---------------------------------------------------------------------------

def update_trust(
    workspace_dir: Path,
    node_label: str,
    model: str,
    decision: str,  # "approved" or "declined"
) -> None:
    """Add or update a trust row in network_trust.md."""
    path = workspace_dir / "network_trust.md"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_row = f"| {node_label:<13} | {model:<20} | {decision:<9} | {now} |"

    if not path.exists():
        path.write_text(
            "# Network Trust Registry\n\n"
            "## Remembered approvals\n"
            "| node          | model                | decision  | remembered_at       |\n"
            "|---------------|----------------------|-----------|---------------------|\n"
            f"{new_row}\n\n"
            "## Permanently revoked\n"
            "| node          | model                | reason                           |\n"
            "|---------------|----------------------|----------------------------------|\n",
            encoding="utf-8",
        )
        return

    content = path.read_text(encoding="utf-8")
    key = f"| {node_label}"
    # Remove existing row for this (node, model) pair
    new_lines = []
    skip_next = False
    for line in content.splitlines():
        stripped = line.strip()
        if (stripped.startswith(f"| {node_label}") and
                f"| {model}" in stripped and
                "## Permanently revoked" not in content.split(line)[0]):
            continue  # remove stale row
        new_lines.append(line)

    # Insert after the separator in Remembered approvals section
    result = []
    inserted = False
    in_approved = False
    past_sep = False
    for line in new_lines:
        result.append(line)
        if "## Remembered approvals" in line:
            in_approved = True
        if in_approved and line.strip().startswith("|---") and not inserted:
            result.append(new_row)
            inserted = True
        if in_approved and "##" in line and "Remembered" not in line:
            in_approved = False

    if not inserted:
        result.append(new_row)

    path.write_text("\n".join(result), encoding="utf-8")


# ---------------------------------------------------------------------------
# Startup approval flow
# ---------------------------------------------------------------------------

def run_startup_approval(
    scored: list[ScoredRemoteModel],
    net_cfg: NetworkConfig,
    workspace_dir: Path,
    console: Console,
) -> dict[tuple[str, str], str]:
    """
    Show startup recommendations, ask user for each pending/better model.
    Returns {(node_label, model): "approved"|"declined"} for this session only.
    """
    session_decisions: dict[tuple[str, str], str] = {}

    if not scored:
        return session_decisions

    # Split into: better recommendations, approved-no-competition, pending-new
    better = [s for s in scored if s.is_better and s.trust != "declined"]
    pending = [s for s in scored if s.trust == "pending" and not s.is_better]
    approved_no_comp = [s for s in scored if s.trust == "approved" and not s.is_better]

    if not better and not pending:
        # Just list what's available
        if approved_no_comp and net_cfg.notify_on_discovery:
            names = ", ".join(f"{s.model}@{s.node_label}" for s in approved_no_comp)
            console.print(f"[dim][network] Approved remote models available: {names}[/dim]")
            for s in approved_no_comp:
                session_decisions[(s.node_label, s.model)] = "approved"
        return session_decisions

    console.print()

    # Better recommendations
    for s in better:
        panel_text = (
            f"  Role:   {s.role}\n"
            f"  Local:  (local model for this role)\n"
            f"  Remote: {s.model} ({s.size_gb} GB) on {s.node_label}\n"
            f"  Diff:   {s.vs_local}"
        )
        console.print(Panel(panel_text, title=f"[bold yellow]Better remote option found[/bold yellow]"))

        prior = s.trust  # "approved" or "declined" from trust file, or "pending"
        default = prior == "approved"
        use_it = Confirm.ask(
            f"  Use remote [cyan]{s.model}[/cyan] for [bold]{s.role}[/bold] tasks this session?",
            default=default,
        )
        decision = "approved" if use_it else "declined"
        session_decisions[(s.node_label, s.model)] = decision

        if net_cfg.remember_approvals:
            remember = Confirm.ask("  Remember this choice?", default=False)
            if remember:
                update_trust(workspace_dir, s.node_label, s.model, decision)

    # Previously declined (shown so user can change)
    declined = [s for s in scored if s.trust == "declined" and s.is_better]
    for s in declined:
        change = Confirm.ask(
            f"  [dim]{s.model} on {s.node_label} — remembered: declined. Change?[/dim]",
            default=False,
        )
        if change:
            use_it = Confirm.ask(f"  Approve {s.model}?", default=False)
            decision = "approved" if use_it else "declined"
            session_decisions[(s.node_label, s.model)] = decision
            if net_cfg.remember_approvals:
                remember = Confirm.ask("  Remember?", default=False)
                if remember:
                    update_trust(workspace_dir, s.node_label, s.model, decision)
        else:
            session_decisions[(s.node_label, s.model)] = "declined"

    # New pending models
    for s in pending:
        console.print(
            f"\n  [yellow]New model requires approval:[/yellow] "
            f"[cyan]{s.model}[/cyan] on {s.node_label}"
        )
        if s.description:
            console.print(f"  Description: \"{s.description}\"")
        approve = Confirm.ask("  Approve for this session?", default=False)
        decision = "approved" if approve else "declined"
        session_decisions[(s.node_label, s.model)] = decision
        if net_cfg.remember_approvals and approve:
            remember = Confirm.ask("  Remember this choice?", default=False)
            if remember:
                update_trust(workspace_dir, s.node_label, s.model, decision)

    # Pass through already-approved models
    for s in scored:
        key = (s.node_label, s.model)
        if key not in session_decisions and s.trust == "approved":
            session_decisions[key] = "approved"

    # Print session summary
    approved_this_session = [
        f"{s.model}@{s.node_label}"
        for s in scored
        if session_decisions.get((s.node_label, s.model)) == "approved"
    ]
    if approved_this_session:
        console.print(
            f"\n[dim][network] Session starting with: local models + "
            f"{', '.join(approved_this_session)}[/dim]\n"
        )
    else:
        console.print("[dim][network] Using local models only this session.[/dim]\n")

    return session_decisions


# ---------------------------------------------------------------------------
# Public entry point called from main.py
# ---------------------------------------------------------------------------

def run_network_scan(
    workspace_dir: Path,
    local_fit_rows: list[dict],
    console: Console,
) -> tuple[dict[tuple[str, str], str], list[ScoredRemoteModel], NetworkConfig]:
    """
    Full startup scan flow. Returns:
        session_decisions  — {(node_label, model): "approved"|"declined"}
        scored_models      — all scored remote models (for model_switcher)
        net_cfg            — parsed NetworkConfig (for preference threshold etc.)
    """
    net_cfg = parse_network_config(workspace_dir)
    if not net_cfg.nodes:
        return {}, [], net_cfg

    console.print("[dim][network] Scanning remote nodes...[/dim]", end="")
    nodes = scan_nodes(net_cfg)

    online = [n for n in nodes if n.online]
    offline = [n for n in nodes if not n.online]
    console.print(
        f"\r[dim][network] {len(online)}/{len(nodes)} nodes online"
        + (f" | offline: {', '.join(n.label for n in offline)}" if offline else "")
        + "[/dim]"
    )

    trust = parse_trust_registry(workspace_dir)
    scored = build_registry(net_cfg, nodes, local_fit_rows, trust, workspace_dir)

    if not scored:
        return {}, [], net_cfg

    session_decisions = run_startup_approval(scored, net_cfg, workspace_dir, console)
    return session_decisions, scored, net_cfg
