#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
source .venv/bin/activate

# --cli flag falls back to the original terminal mode (no TUI)
if [[ "$1" == "--cli" ]]; then
    shift
    python main.py "$@"
else
    python tui.py "$@"
fi
