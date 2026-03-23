# Ora OS — Multimodal & Vision Routing Specification v0.1

### O.R.A. — Orchestrated Reasoning Agent

> Companion spec to `project_spec.md`, `network_spec.md`, and `settings_spec.md`.
> Defines how Ora OS detects, routes, and processes image and file attachments
> using a two-stage vision pipeline.

---

## Overview

Ora OS supports image and file attachments through a **pre-routing layer** that runs
before any LLM call. The active instruct model (e.g. `qwen3:4b`) cannot process images —
it only handles text. When an attachment is detected, the Python framework intercepts it,
routes it to a vision-capable model first, then passes the resulting text description to
the instruct model for reasoning and action.

**Core principles:**
- The LLM never decides whether an image is present — the Python routing layer does.
- Vision and instruct are always separate models — one sees the image, one reasons about it.
- If no vision model is configured, Ora responds gracefully rather than silently failing.
- The capability flag in `viable_models.md` is the single source of truth for what each
  model can handle.
- This design works identically for local and remote models — a remote vision model on
  the network is valid and follows the same routing logic.

---

## Changes to Existing Files

### `workspace/viable_models.md` — add `capabilities` column

```markdown
# Viable Models

| model              | size_gb | role      | capabilities  | notes                          | auto_pull |
|--------------------|---------|-----------|---------------|--------------------------------|-----------|
| qwen3:4b           | 2.5     | instruct  | text          | default model, strong tool use | yes       |
| qwen2.5-vl:3b      | 2.0     | vision    | text,images   | vision model, Jetson-optimised | yes       |
| deepseek-r1:1.5b   | 1.0     | reasoning | text          | light reasoning, slow          | yes       |
| phi4-mini:3.8b     | 2.3     | fast      | text          | fast alternative instruct      | yes       |
```

**Valid capability values:**
- `text` — text only, cannot process images
- `text,images` — multimodal, can process both text and images

The routing layer reads this column before every call. A model with only `text` capability
will never receive image data regardless of what the user sends.

---

### `workspace/model_roles.md` — add vision role

```markdown
### vision
model: qwen2.5-vl:3b
capabilities: text,images
use_when: >
  The user has attached an image, screenshot, photo, or visual file.
  This model describes the visual content as detailed text, which is then
  passed to the instruct model for reasoning and action.
  Do NOT use for text-only tasks — it is weaker at reasoning than the instruct model.
example_trigger: "user uploads screenshot", "user attaches photo", "user sends image file"
vision_strategy: describe_then_reason
  # describe_then_reason: vision model describes → instruct model reasons (default)
  # vision_handles_all:   vision model handles the full response (simple visual Q&A only)
```

---

## New File: `workspace/vision_config.md`

User-editable. Controls vision routing behaviour.

```markdown
# Vision Config

## Strategy
# describe_then_reason (default): vision model describes the image as text,
#   then the instruct model reasons about it. Best for complex tasks.
# vision_handles_all: vision model generates the full response directly.
#   Use only for simple "what is in this image?" queries.
default_vision_strategy: describe_then_reason

## Description prompt
# Sent to the vision model when an image is received.
# Keep it generic — it needs to work for any image type.
vision_description_prompt: >
  Describe this image in precise detail. If it contains text, code, terminal output,
  error messages, logs, or UI elements, transcribe them exactly.
  If it contains a diagram, chart, or visual data, describe the structure and values.
  Be thorough — your description will be used by another model to answer the user's question.

## File types treated as images
image_extensions: .png, .jpg, .jpeg, .gif, .webp, .bmp, .tiff

## File types passed as text (no vision model needed)
text_extensions: .txt, .md, .py, .js, .ts, .json, .yaml, .yml, .log, .csv, .sh

## File types not supported in v1
unsupported_extensions: .pdf, .docx, .xlsx, .zip
# Note: PDF and document support planned for v2 via text extraction tools

## Fallback behaviour when no vision model is configured
no_vision_model_response: >
  I received an image but no vision-capable model is configured.
  To enable image understanding, add a model with capabilities: text,images
  to workspace/viable_models.md and assign it the 'vision' role in model_roles.md.
  Suggested model for your hardware: qwen2.5-vl:3b
```

---

## Routing Logic

### Pre-routing check (runs before every LLM call)

```python
def route_message(user_message, attachments):

    image_attachments = [f for f in attachments
                         if get_extension(f) in IMAGE_EXTENSIONS]
    text_attachments  = [f for f in attachments
                         if get_extension(f) in TEXT_EXTENSIONS]
    bad_attachments   = [f for f in attachments
                         if get_extension(f) not in IMAGE_EXTENSIONS + TEXT_EXTENSIONS]

    # Unsupported file types — notify user immediately
    if bad_attachments:
        notify_user(f"Unsupported file type(s): {bad_attachments}. "
                    f"Supported: images and plain text files.")

    # Text files — read content and inject into message as text
    if text_attachments:
        for f in text_attachments:
            user_message += f"\n\n[File: {f.name}]\n{read_file(f)}"

    # Images — trigger vision pipeline
    if image_attachments:
        vision_model = get_model_by_role("vision")

        if vision_model is None:
            return no_vision_model_response()

        if vision_config.default_vision_strategy == "describe_then_reason":
            return describe_then_reason(
                user_message, image_attachments, vision_model)
        else:
            return vision_handles_all(
                user_message, image_attachments, vision_model)

    # No attachments — normal instruct model call
    return call_instruct_model(user_message)
```

---

## Two-Stage Vision Pipeline

### Strategy 1: `describe_then_reason` (default — recommended)

Best for: error screenshots, log files as images, diagrams, charts, UI screenshots,
any case where the user wants Ora to *do something* with the image content.

```
Stage 1 — Vision model describes the image
  Input:  image + vision_description_prompt
  Model:  qwen2.5-vl:3b (or configured vision model)
  Output: detailed text description

Stage 2 — Instruct model reasons and acts
  Input:  original user message + "[Image description: {description}]"
  Model:  qwen3:4b (active instruct model)
  Output: reasoning, tool calls, final answer — normal agent loop continues
```

**Example — systemd failure screenshot:**

```
User: [screenshot.png] why is my service failing?

Stage 1 → qwen2.5-vl:3b receives:
  "Describe this image in precise detail. If it contains text, terminal output..."
  + screenshot.png

  Returns: "Terminal output showing 'systemctl status ora.service'. Status is
  'failed'. Journal shows: 'ExecStart=/opt/ora_os/.venv/bin/python main.py'.
  Error: 'exit code 203/EXEC'. Last line: 'Process exited with status 203'."

Stage 2 → qwen3:4b receives:
  "[Image description]: Terminal output showing systemctl status ora.service.
  Status failed. ExecStart=/opt/ora_os/.venv/bin/python main.py.
  Error: exit code 203/EXEC."
  User question: "why is my service failing?"

  Reasons: exit code 203 means the executable path wasn't found or not executable.
  Calls: run_bash("ls -la /opt/ora_os/.venv/bin/python")
  Calls: run_bash("systemctl cat ora.service")
  Returns: full diagnosis and fix suggestion
```

The user sees a single coherent response. The two-stage pipeline is invisible to them
except for a brief status line: `[ora] Reading image... done. Reasoning...`

---

### Strategy 2: `vision_handles_all`

Best for: simple visual Q&A where no action or tool use is needed.
Example: "what colour is the button in this screenshot?" or "describe this photo."

```
Single call → qwen2.5-vl:3b receives:
  user message + image

  Returns: direct answer, shown to user immediately.
  No instruct model involved.
```

Switch to this strategy in `vision_config.md` when you want faster responses for
simple image questions and don't need tool use or complex reasoning.

---

## Session State Update

`workspace/session_state.md` gains a vision section:

```markdown
## Vision activity (this session)
| time     | file              | strategy             | vision model     | instruct model |
|----------|-------------------|----------------------|------------------|----------------|
| 10:03:12 | screenshot.png    | describe_then_reason | qwen2.5-vl:3b    | qwen3:4b       |
| 10:07:44 | error_log.png     | describe_then_reason | qwen2.5-vl:3b    | qwen3:4b       |
```

---

## Hardware Tier Presets

### Jetson Orin Nano Super 8GB (this hardware)

Memory budget: ~5.5GB usable for models (OS + Ollama overhead ~2.5GB)
Only one model loaded at a time — Ollama hot-swaps between calls.

```markdown
| model            | size_gb | role      | capabilities | fits   |
|------------------|---------|-----------|--------------|--------|
| qwen3:4b         | 2.5     | instruct  | text         | ✅     |
| qwen2.5-vl:3b    | 2.0     | vision    | text,images  | ✅     |
| deepseek-r1:1.5b | 1.0     | reasoning | text         | ✅     |
| phi4-mini:3.8b   | 2.3     | fast      | text         | ✅     |
```

All four fit individually. They cannot co-load — Ollama unloads between calls.
Token speed estimate on Jetson: ~20-35 tok/s for 3-4B models at Q4 quantization.

### Mid-range desktop (e.g. RTX 3080 10GB)

```markdown
| model                | size_gb | role      | capabilities | fits   |
|----------------------|---------|-----------|--------------|--------|
| qwen3:8b             | 5.0     | instruct  | text         | ✅     |
| qwen2.5-vl:7b        | 5.0     | vision    | text,images  | ✅     |
| deepseek-r1:7b       | 4.5     | reasoning | text         | ✅     |
| qwen3:4b             | 2.5     | fast      | text         | ✅     |
```

qwen3:8b + qwen2.5-vl:7b cannot co-load (10GB total). Hot-swap required.

### High-end desktop (e.g. RTX 4090 24GB)

```markdown
| model                | size_gb | role      | capabilities | fits   |
|----------------------|---------|-----------|--------------|--------|
| qwen3-coder:30b      | 18.5    | instruct  | text         | ✅     |
| qwen2.5-vl:7b        | 5.0     | vision    | text,images  | ✅     |
| deepseek-r1:14b      | 9.0     | reasoning | text         | ✅     |
| qwen3:4b             | 2.5     | fast      | text         | ✅     |
```

qwen3-coder:30b + qwen2.5-vl:7b = 23.5GB → ✅ can co-load on 24GB VRAM.
Vision pipeline runs without unloading the instruct model — faster two-stage calls.

---

## Graceful Failure Cases

| Situation | Ora's response |
|---|---|
| Image uploaded, no vision model in `viable_models.md` | Prints `no_vision_model_response` from `vision_config.md`, suggests `qwen2.5-vl:3b` |
| Vision model listed but not pulled in Ollama | Offers to pull it via `pull_model()` if `auto_pull: yes`, otherwise notifies user |
| Vision model listed but doesn't fit in VRAM/RAM | Falls back to text-only response, notifies user |
| Unsupported file type (.pdf, .docx) | Notifies user, lists supported types, notes PDF support planned for v2 |
| Text file attached (.py, .log, .md) | Reads file content directly as text, no vision model needed |
| Image too large (>10MB) | Warns user, attempts anyway, catches OOM error gracefully |
| Vision model returns empty description | Retries once, then notifies user the image could not be processed |

---

## v1 Scope Boundaries

| In scope | Out of scope (future versions) |
|---|---|
| PNG, JPG, WEBP, GIF image routing | PDF text extraction and processing |
| Two-stage describe-then-reason pipeline | OCR on scanned documents |
| Plain text file injection (.py, .log, .md) | Audio / video file processing |
| Per-model capability flag in viable_models.md | Automatic capability detection from model metadata |
| Graceful fallback when no vision model configured | Multi-image batching in a single call |
| Remote vision model support (via network_spec) | Real-time camera / video stream input |
| Hardware tier presets in docs | Auto hardware-tier detection and preset selection |

---

## Dependencies Added

No new dependencies required. Image bytes are passed directly to Ollama's
`/v1/chat/completions` endpoint as base64 in the `image_url` field of the
message content array — this is already supported by Ollama for vision models
and requires no additional Python packages beyond what is already in
`requirements.txt`.

```python
# How the vision model call looks in code
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": vision_config.vision_description_prompt
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_image}"
                }
            }
        ]
    }
]
response = ollama_client.chat(model="qwen2.5-vl:3b", messages=messages)
description = response["message"]["content"]
```
