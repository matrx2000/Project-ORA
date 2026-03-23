"""
vision_router.py — Pre-routing layer for image and file attachments.

Detects file paths in user messages, classifies by extension, routes images
to the vision model (two-stage pipeline), and injects text files inline.
Runs before every LLM call in main.py.
"""
import base64
import datetime
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from openai import OpenAI
from rich.console import Console

from tools.hardware_probe import parse_viable_models


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class VisionResult:
    """Return value from route_user_message."""
    message: str                          # processed message for the instruct model
    vision_logs: list[dict] = field(default_factory=list)
    is_direct_response: bool = False      # True when vision_handles_all produced the final answer


# ---------------------------------------------------------------------------
# Vision config parser
# ---------------------------------------------------------------------------

def parse_vision_config(workspace_dir: Path) -> dict:
    """Parse workspace/vision_config.md into a config dict."""
    defaults = {
        "default_vision_strategy": "describe_then_reason",
        "vision_description_prompt": (
            "Describe this image in precise detail. If it contains text, code, "
            "terminal output, error messages, logs, or UI elements, transcribe them exactly. "
            "If it contains a diagram, chart, or visual data, describe the structure and values. "
            "Be thorough — your description will be used by another model to answer the user's question."
        ),
        "image_extensions": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"],
        "text_extensions": [".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".log", ".csv", ".sh"],
        "unsupported_extensions": [".pdf", ".docx", ".xlsx", ".zip"],
        "no_vision_model_response": (
            "I received an image but no vision-capable model is configured. "
            "To enable image understanding, add a model with capabilities: text,images "
            "to workspace/viable_models.md and assign it the 'vision' role in model_roles.md."
        ),
    }

    path = workspace_dir / "vision_config.md"
    if not path.exists():
        return defaults

    content = path.read_text(encoding="utf-8")
    multiline_key = None
    multiline_parts = []

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            if multiline_key and not (line.startswith("  ") or line.startswith("\t")):
                defaults[multiline_key] = " ".join(multiline_parts).strip()
                multiline_key = None
                multiline_parts = []
            continue

        if multiline_key and (line.startswith("  ") or line.startswith("\t")):
            multiline_parts.append(stripped)
            continue

        if multiline_key:
            defaults[multiline_key] = " ".join(multiline_parts).strip()
            multiline_key = None
            multiline_parts = []

        if ": >" in stripped:
            key = stripped.split(": >")[0].strip()
            multiline_key = key
            multiline_parts = []
        elif ": " in stripped:
            key, value = stripped.split(": ", 1)
            key, value = key.strip(), value.strip()
            if key in ("image_extensions", "text_extensions", "unsupported_extensions"):
                defaults[key] = [e.strip() for e in value.split(",") if e.strip()]
            else:
                defaults[key] = value

    if multiline_key:
        defaults[multiline_key] = " ".join(multiline_parts).strip()

    return defaults


# ---------------------------------------------------------------------------
# File path detection in user messages
# ---------------------------------------------------------------------------

# Match absolute paths, ~/paths, and ./relative paths ending with an extension
_FILE_PATH_PATTERN = re.compile(
    r'(?:^|\s|["\'])'                   # preceded by whitespace, quote, or start
    r'((?:[/~.][\w./\\-]+)'             # path starting with / ~ or .
    r'\.(\w{1,5}))'                      # dot + extension
    r'(?:\s|["\']|$)',                   # followed by whitespace, quote, or end
)


def extract_file_paths(text: str) -> list[str]:
    """Extract file paths from user message text."""
    paths = []
    for match in _FILE_PATH_PATTERN.finditer(text):
        full_path = match.group(1)
        # Expand ~ to home
        expanded = Path(full_path).expanduser()
        paths.append(str(expanded))
    return paths


def classify_paths(
    paths: list[str],
    image_exts: list[str],
    text_exts: list[str],
    unsupported_exts: list[str],
) -> tuple[list[Path], list[Path], list[Path]]:
    """Classify file paths into (images, text_files, unsupported)."""
    images, texts, unsupported = [], [], []
    for p_str in paths:
        p = Path(p_str)
        ext = p.suffix.lower()
        if ext in image_exts:
            images.append(p)
        elif ext in text_exts:
            texts.append(p)
        elif ext in unsupported_exts:
            unsupported.append(p)
        # Unknown extensions are silently ignored (might not be file paths at all)
    return images, texts, unsupported


# ---------------------------------------------------------------------------
# Vision model lookup
# ---------------------------------------------------------------------------

def get_vision_model(workspace_dir: Path) -> dict | None:
    """Find the vision-capable model from viable_models.md. Returns model dict or None."""
    viable = parse_viable_models(workspace_dir)
    for m in viable:
        caps = m.get("capabilities", "text")
        if "images" in caps:
            return m
    return None


# ---------------------------------------------------------------------------
# Image encoding
# ---------------------------------------------------------------------------

def _encode_image(image_path: Path) -> tuple[str, str]:
    """Read and base64-encode an image file. Returns (base64_str, mime_type)."""
    data = image_path.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    return b64, mime


# ---------------------------------------------------------------------------
# Vision pipeline calls
# ---------------------------------------------------------------------------

def _call_vision_model(
    ollama_base_url: str,
    vision_model_name: str,
    prompt: str,
    image_paths: list[Path],
) -> str:
    """Send images + prompt to the vision model, return text description."""
    client = OpenAI(
        base_url=ollama_base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )

    content_parts = [{"type": "text", "text": prompt}]
    for img_path in image_paths:
        try:
            b64, mime = _encode_image(img_path)
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        except Exception as exc:
            content_parts.append({
                "type": "text",
                "text": f"[Could not read {img_path.name}: {exc}]",
            })

    try:
        response = client.chat.completions.create(
            model=vision_model_name,
            messages=[{"role": "user", "content": content_parts}],
            extra_body={"keep_alive": "0s"},
        )
        result = response.choices[0].message.content or ""
        if not result.strip():
            # Retry once on empty
            response = client.chat.completions.create(
                model=vision_model_name,
                messages=[{"role": "user", "content": content_parts}],
                extra_body={"keep_alive": "0s"},
            )
            result = response.choices[0].message.content or ""
            if not result.strip():
                return "[Vision model returned empty description — image could not be processed]"
        return result
    except Exception as exc:
        return f"[Vision model error: {exc}]"


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def route_user_message(
    user_message: str,
    workspace_dir: Path,
    ollama_base_url: str,
    active_model: str,
    console: Console,
) -> VisionResult:
    """
    Pre-process a user message for file attachments and vision routing.

    Returns a VisionResult:
        .message            — the processed message for the instruct model
        .vision_logs        — list of dicts for session_state.md
        .is_direct_response — True when vision_handles_all produced the final answer
    """
    vcfg = parse_vision_config(workspace_dir)

    # Extract and classify file paths
    raw_paths = extract_file_paths(user_message)
    if not raw_paths:
        return VisionResult(message=user_message)

    images, text_files, unsupported = classify_paths(
        raw_paths,
        vcfg["image_extensions"],
        vcfg["text_extensions"],
        vcfg["unsupported_extensions"],
    )

    # Handle unsupported files
    if unsupported:
        names = ", ".join(p.name for p in unsupported)
        console.print(
            f"[yellow][ora] Unsupported file type(s): {names}. "
            f"Supported: images ({', '.join(vcfg['image_extensions'])}) "
            f"and text files ({', '.join(vcfg['text_extensions'][:5])}...).[/yellow]"
        )

    # Inject text file contents
    message = user_message
    for tf in text_files:
        if tf.exists():
            try:
                content = tf.read_text(encoding="utf-8", errors="replace")
                if len(content) > 50_000:
                    content = content[:50_000] + "\n... [truncated at 50KB]"
                message += f"\n\n[File: {tf.name}]\n```\n{content}\n```"
                console.print(f"[dim][ora] Read text file: {tf.name}[/dim]")
            except Exception as exc:
                message += f"\n\n[Could not read {tf.name}: {exc}]"
        else:
            message += f"\n\n[File not found: {tf}]"

    # Handle images — vision pipeline
    existing_images = [img for img in images if img.exists()]
    missing_images = [img for img in images if not img.exists()]

    for mi in missing_images:
        message += f"\n\n[Image file not found: {mi}]"

    if not existing_images:
        return VisionResult(message=message)

    # Check for oversized images (>10MB)
    for img in existing_images:
        size_mb = img.stat().st_size / (1024 * 1024)
        if size_mb > 10:
            console.print(
                f"[yellow][ora] Warning: {img.name} is {size_mb:.1f}MB — "
                f"large images may cause memory issues.[/yellow]"
            )

    # Find vision model
    vision_model = get_vision_model(workspace_dir)
    if vision_model is None:
        no_vision = vcfg.get("no_vision_model_response", "No vision model configured.")
        console.print(f"[yellow][ora] {no_vision}[/yellow]")
        message += f"\n\n[{no_vision}]"
        return VisionResult(message=message)

    vision_model_name = vision_model["model"]
    strategy = vcfg.get("default_vision_strategy", "describe_then_reason")
    description_prompt = vcfg.get("vision_description_prompt", "Describe this image.")

    if strategy == "vision_handles_all":
        console.print(f"[dim][ora] Processing image with {vision_model_name}...[/dim]")
        result = _call_vision_model(
            ollama_base_url, vision_model_name, user_message, existing_images,
        )
        vision_logs = [
            {"file": img.name, "strategy": "vision_handles_all",
             "vision_model": vision_model_name, "instruct_model": "—"}
            for img in existing_images
        ]
        return VisionResult(message=result, vision_logs=vision_logs, is_direct_response=True)

    # Default: describe_then_reason
    console.print(f"[dim][ora] Reading image{'s' if len(existing_images) > 1 else ''}... [/dim]", end="")
    description = _call_vision_model(
        ollama_base_url, vision_model_name, description_prompt, existing_images,
    )
    console.print(f"[dim]done. Reasoning...[/dim]")

    image_names = ", ".join(img.name for img in existing_images)
    message += f"\n\n[Image description ({image_names})]: {description}"

    vision_logs = [
        {"file": img.name, "strategy": "describe_then_reason",
         "vision_model": vision_model_name, "instruct_model": active_model}
        for img in existing_images
    ]
    return VisionResult(message=message, vision_logs=vision_logs)
