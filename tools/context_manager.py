"""
context_manager.py — Transparent context overflow detection and compaction.
Called after every LLM response; invisible to the model.
"""
import datetime
from pathlib import Path

import tiktoken
from openai import OpenAI
from rich.console import Console


# Fallback context windows for common Ollama models
_CONTEXT_WINDOWS: dict[str, int] = {
    "phi4-mini": 16384,
    "qwen3-coder:30b": 32768,
    "deepseek-r1:14b": 32768,
    "qwen3:4b-instruct": 32768,
    "devstral:latest": 32768,
}
_DEFAULT_CONTEXT_WINDOW = 32768

_TOKENIZER = None


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TOKENIZER = None
    return _TOKENIZER


def count_tokens(text: str) -> int:
    """Approximate token count using cl100k_base tokenizer."""
    enc = _get_tokenizer()
    if enc is None:
        # Rough fallback: ~4 chars per token
        return len(text) // 4
    return len(enc.encode(text, disallowed_special=()))


def count_messages_tokens(messages: list) -> int:
    """Count total tokens across all messages."""
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m.get("content", ""))
        total += count_tokens(content) + 4  # ~4 tokens per message overhead
    return total


def _get_context_window(model_name: str) -> int:
    for key, window in _CONTEXT_WINDOWS.items():
        if key in model_name:
            return window
    return _DEFAULT_CONTEXT_WINDOW


def _messages_to_openai(messages: list) -> list[dict]:
    """Convert LangChain messages to OpenAI dicts."""
    result = []
    for m in messages:
        if hasattr(m, "type"):
            role = {"human": "user", "ai": "assistant", "system": "system",
                    "tool": "tool"}.get(m.type, "user")
        else:
            role = m.get("role", "user")
        content = m.content if hasattr(m, "content") else str(m.get("content", ""))
        result.append({"role": role, "content": content})
    return result


def _summarise(
    messages_to_compress: list,
    active_model: str,
    ollama_base_url: str,
    max_summary_tokens: int,
) -> str:
    """Ask the active model to summarise old messages. Returns summary text."""
    client = OpenAI(
        base_url=ollama_base_url.rstrip("/") + "/v1",
        api_key="ollama",
    )
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in _messages_to_openai(messages_to_compress)
        if m["role"] not in ("system",)
    )
    prompt = (
        f"Summarise the following conversation excerpt in no more than {max_summary_tokens} tokens. "
        "Be concise. Capture key facts, decisions, and open tasks. "
        "Do NOT include the summary header — just the summary text.\n\n"
        f"{conversation_text}"
    )
    try:
        resp = client.chat.completions.create(
            model=active_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_summary_tokens + 50,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        return f"[summary unavailable: {exc}]"


def _load_existing_summary(workspace_dir: Path) -> str:
    path = workspace_dir / "memory" / "context_summary.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    # Extract the summary text between ## Summary and next ##
    if "## Summary" in content:
        after = content.split("## Summary", 1)[1]
        if "##" in after:
            return after.split("##")[0].strip()
        return after.strip()
    return ""


def _write_context_summary(workspace_dir: Path, summary: str, overflow_count: int) -> None:
    path = workspace_dir / "memory" / "context_summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"# Context Summary\n"
        f"_Last summarised: {now}_\n"
        f"_Overflow events this session: {overflow_count}_\n\n"
        f"## Summary\n{summary}\n"
    )
    path.write_text(content, encoding="utf-8")


def check_and_compact(
    messages: list,
    active_model: str,
    ollama_base_url: str,
    workspace_dir,
    overflow_threshold: float,
    summary_keep_last_n_turns: int,
    max_summary_tokens: int,
    overflow_count: int,
    console: Console,
) -> tuple[list, int]:
    """
    Check token usage and compact messages if over the threshold.

    Returns:
        (updated_messages, new_overflow_count)
    """
    workspace_dir = Path(workspace_dir)
    context_window = _get_context_window(active_model)
    tokens_used = count_messages_tokens(messages)
    usage_pct = tokens_used / context_window

    if usage_pct < overflow_threshold:
        return messages, overflow_count

    # --- Overflow triggered ---
    overflow_count += 1
    console.print(
        f"[dim][context] {usage_pct:.0%} full — summarising and compacting. "
        f"Continuing...[/dim]"
    )

    # Separate system message from the rest
    system_msgs = [m for m in messages if getattr(m, "type", None) == "system"
                   or (isinstance(m, dict) and m.get("role") == "system")]
    non_system = [m for m in messages if m not in system_msgs]

    # Keep last N turns (each turn = 1 human + 1+ AI messages)
    keep_last = min(summary_keep_last_n_turns * 2, len(non_system))
    to_compress = non_system[:-keep_last] if keep_last < len(non_system) else []
    tail = non_system[-keep_last:] if keep_last > 0 else non_system

    if not to_compress:
        # Nothing to compress yet — can't reduce further
        return messages, overflow_count

    # Summarise the compressible part
    summary_text = _summarise(to_compress, active_model, ollama_base_url, max_summary_tokens)

    # Save to disk
    _write_context_summary(workspace_dir, summary_text, overflow_count)

    # Rebuild messages: system + summary injection + tail
    from langchain_core.messages import SystemMessage

    summary_injection = SystemMessage(
        content=f"[Previous context summary]\n{summary_text}"
    )
    new_messages = system_msgs + [summary_injection] + tail

    return new_messages, overflow_count


def get_token_stats(messages: list, active_model: str) -> dict:
    """Return a dict with token usage stats for session_state.md."""
    context_window = _get_context_window(active_model)
    tokens_used = count_messages_tokens(messages)
    return {
        "context_window": context_window,
        "tokens_used": tokens_used,
        "tokens_used_pct": round(100 * tokens_used / context_window, 1),
    }
