"""
hardware_probe.py — Detect CPU, RAM, GPU and score viable models against hardware.
Called automatically on every boot. Not a model-callable tool.
"""
import subprocess
import platform
import datetime
import json
import urllib.request
from pathlib import Path

import psutil

try:
    import pynvml
    _PYNVML_AVAILABLE = True
except ImportError:
    _PYNVML_AVAILABLE = False


# ---------------------------------------------------------------------------
# GPU backend detectors
# ---------------------------------------------------------------------------

def _detect_nvidia() -> list[dict]:
    if not _PYNVML_AVAILABLE:
        return []
    try:
        pynvml.nvmlInit()
        gpus = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpus.append({
                "vendor": "NVIDIA",
                "model": name,
                "vram_total_gb": round(mem.total / 1024 ** 3, 1),
                "vram_available_gb": round(mem.free / 1024 ** 3, 1),
                "backend": "cuda",
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        return []


def _detect_amd() -> list[dict]:
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        gpus = []
        for card_id, info in data.items():
            if not card_id.startswith("card"):
                continue
            total_gb = int(info.get("VRAM Total Memory (B)", 0)) / 1024 ** 3
            used_gb = int(info.get("VRAM Total Used Memory (B)", 0)) / 1024 ** 3
            gpus.append({
                "vendor": "AMD",
                "model": info.get("GPU ID", card_id),
                "vram_total_gb": round(total_gb, 1),
                "vram_available_gb": round(total_gb - used_gb, 1),
                "backend": "rocm",
            })
        return gpus
    except Exception:
        return []


def _detect_apple_silicon() -> list[dict]:
    if platform.system() != "Darwin":
        return []
    try:
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType", "-json"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        hw = data.get("SPHardwareDataType", [{}])[0]
        chip = hw.get("chip_type", "")
        if "Apple" not in chip:
            return []
        mem = psutil.virtual_memory()
        total_gb = round(mem.total / 1024 ** 3, 1)
        avail_gb = round(mem.available / 1024 ** 3, 1)
        return [{
            "vendor": "Apple",
            "model": chip,
            "vram_total_gb": total_gb,
            "vram_available_gb": avail_gb,
            "backend": "metal",
        }]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cpu_model() -> str:
    if platform.system() == "Linux":
        try:
            out = subprocess.check_output(
                ["grep", "-m1", "model name", "/proc/cpuinfo"], text=True, timeout=3
            )
            return out.split(":", 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or "Unknown"


def query_ollama_models(base_url: str) -> dict[str, float]:
    """Return {model_name: size_gb} for all pulled Ollama models."""
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        return {
            m["name"]: round(m.get("size", 0) / 1024 ** 3, 1)
            for m in data.get("models", [])
        }
    except Exception:
        return {}


def parse_viable_models(workspace_dir: Path) -> list[dict]:
    """Parse workspace/viable_models.md table rows into list of dicts.

    Supports both old 5-column format and new 6-column format (with capabilities).
    """
    path = workspace_dir / "viable_models.md"
    if not path.exists():
        return []
    models = []
    in_table = False
    header_cols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("| model"):
            header_cols = [p.strip().lower() for p in stripped.split("|")[1:-1]]
            in_table = True
            continue
        if stripped.startswith("|--"):
            continue
        if in_table and stripped.startswith("|"):
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if len(parts) >= 5:
                try:
                    has_caps = "capabilities" in header_cols and len(parts) >= 6
                    if has_caps:
                        # 6-col: model | size_gb | role | capabilities | notes | auto_pull
                        models.append({
                            "model": parts[0],
                            "size_gb": float(parts[1]),
                            "role": parts[2],
                            "capabilities": parts[3],
                            "notes": parts[4],
                            "auto_pull": parts[5].lower() == "yes",
                        })
                    else:
                        # 5-col legacy: model | size_gb | role | notes | auto_pull
                        models.append({
                            "model": parts[0],
                            "size_gb": float(parts[1]),
                            "role": parts[2],
                            "capabilities": "text",
                            "notes": parts[3],
                            "auto_pull": parts[4].lower() == "yes",
                        })
                except ValueError:
                    pass
        elif in_table and not stripped.startswith("|"):
            in_table = False
    return models


def score_models(viable: list[dict], avail_vram_gb: float, avail_ram_gb: float) -> list[dict]:
    """Return viable list with fits_vram / fits_ram added."""
    has_gpu = avail_vram_gb > 0
    scored = []
    for m in viable:
        scored.append({
            **m,
            "fits_vram": has_gpu and avail_vram_gb >= m["size_gb"],
            "fits_ram": avail_ram_gb >= m["size_gb"],
        })
    return scored


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def probe_hardware(workspace_dir, ollama_base_url: str = "http://127.0.0.1:11434"):
    """
    Probe hardware, score viable models, write hardware_profile.md.

    Returns:
        summary_str  — compact one-liner for injection into the system prompt
        fit_rows     — list of dicts with model fit information
    """
    workspace_dir = Path(workspace_dir)

    # CPU + RAM
    cpu = _cpu_model()
    cpu_cores = psutil.cpu_count(logical=True)
    mem = psutil.virtual_memory()
    ram_total_gb = round(mem.total / 1024 ** 3, 1)
    ram_avail_gb = round(mem.available / 1024 ** 3, 1)

    # GPU detection: NVIDIA > AMD > Apple Silicon > CPU-only
    gpus = _detect_nvidia() or _detect_amd() or _detect_apple_silicon()
    backend = gpus[0]["backend"] if gpus else "cpu"

    avail_vram = sum(g["vram_available_gb"] for g in gpus)
    total_vram = sum(g["vram_total_gb"] for g in gpus)

    # Viable models from workspace (may not exist on first run)
    viable = parse_viable_models(workspace_dir)
    fit_rows = score_models(viable, avail_vram, ram_avail_gb)

    # Parallel load feasibility (top 10 pairs to keep file manageable)
    parallel_lines = []
    for i, a in enumerate(viable):
        for b in viable[i + 1:]:
            combined = a["size_gb"] + b["size_gb"]
            fits = avail_vram >= combined if gpus else ram_avail_gb >= combined
            sym = "yes" if fits else "no"
            parallel_lines.append(
                f"{a['model']} ({a['size_gb']}) + {b['model']} ({b['size_gb']}) "
                f"= {combined} GB -> fits: {sym}"
            )
    parallel_lines = parallel_lines[:10]

    # Build hardware_profile.md
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# Hardware Profile\n_Generated: {now}_\n"]

    lines.append(
        f"## CPU\nmodel: {cpu}\ncores: {cpu_cores}\n"
        f"ram_total_gb: {ram_total_gb}\nram_available_gb: {ram_avail_gb}\n"
    )

    if gpus:
        for i, g in enumerate(gpus):
            lines.append(
                f"## GPU {i}\nvendor: {g['vendor']}\nmodel: {g['model']}\n"
                f"vram_total_gb: {g['vram_total_gb']}\nvram_available_gb: {g['vram_available_gb']}\n"
                f"backend: {g['backend']}\n"
            )
    else:
        lines.append("## GPU 0\nnot present\n")

    lines.append(f"## Ollama GPU backend\ndetected: {backend}\nfallback: cpu\n")

    if fit_rows:
        lines.append("## Model fit summary")
        lines.append("| model | size_gb | fits_vram | fits_ram |")
        lines.append("|-------|---------|-----------|----------|")
        for r in fit_rows:
            vram_sym = "yes" if r["fits_vram"] else "no"
            ram_sym = "yes" if r["fits_ram"] else "no"
            lines.append(f"| {r['model']:<28} | {r['size_gb']:<7} | {vram_sym:<9} | {ram_sym} |")
        lines.append("")

    if parallel_lines:
        lines.append("## Parallel load feasibility")
        lines.extend(parallel_lines)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "hardware_profile.md").write_text("\n".join(lines), encoding="utf-8")

    # Compact one-liner for system prompt injection
    if gpus:
        gpu_desc = ", ".join(
            f"{g['model']} ({g['vram_available_gb']}GB VRAM free)" for g in gpus
        )
    else:
        gpu_desc = "CPU only"

    summary = (
        f"CPU: {cpu} ({cpu_cores} cores) | "
        f"RAM: {ram_avail_gb}/{ram_total_gb} GB free | "
        f"GPU: {gpu_desc} | backend: {backend}"
    )
    return summary, fit_rows
