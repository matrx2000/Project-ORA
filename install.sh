#!/usr/bin/env bash
# -----------------------------------------------------------------------
# Ora OS — install script
# Sets up Python venv, installs dependencies, and verifies prerequisites.
# Usage:  chmod +x install.sh && ./install.sh
# -----------------------------------------------------------------------
set -euo pipefail

VENV_DIR=".venv"
MIN_PYTHON="3.11"
REQUIREMENTS="requirements.txt"

# -- colours (no-op if not a terminal) ----------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi

info()  { echo -e "${CYAN}[ora]${NC} $*"; }
ok()    { echo -e "${GREEN}[ora]${NC} $*"; }
warn()  { echo -e "${YELLOW}[ora]${NC} $*"; }
fail()  { echo -e "${RED}[ora]${NC} $*"; exit 1; }

# -- change to script directory -----------------------------------------
cd "$(dirname "$0")"
info "Installing Ora OS from $(pwd)"
echo

# -----------------------------------------------------------------------
# 1. Check Python
# -----------------------------------------------------------------------
info "Checking Python..."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        if [ -n "$ver" ]; then
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                PYTHON="$candidate"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python >= $MIN_PYTHON is required but not found.
       Install it with:  sudo apt install python3.11 python3.11-venv  (Ubuntu/Debian)
                    or:  sudo dnf install python3.11  (Fedora)
                    or:  https://www.python.org/downloads/"
fi

ok "Found $PYTHON ($("$PYTHON" --version 2>&1))"

# -----------------------------------------------------------------------
# 2. Check Ollama
# -----------------------------------------------------------------------
info "Checking Ollama..."

if command -v ollama &>/dev/null; then
    OLLAMA_VER=$(ollama --version 2>&1 || true)
    ok "Found ollama ($OLLAMA_VER)"
else
    warn "Ollama not found on PATH."
    warn "Ora OS requires Ollama to run LLMs locally."
    warn "Install it:  curl -fsSL https://ollama.com/install.sh | sh"
    echo
fi

# Check if Ollama is running
if curl -sf http://127.0.0.1:11434/api/tags &>/dev/null; then
    MODEL_COUNT=$(curl -sf http://127.0.0.1:11434/api/tags | "$PYTHON" -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "0")
    ok "Ollama is running ($MODEL_COUNT model(s) pulled)"
else
    warn "Ollama is not running (cannot reach http://127.0.0.1:11434)."
    warn "Start it with:  ollama serve"
    echo
fi

# -----------------------------------------------------------------------
# 3. Create virtual environment
# -----------------------------------------------------------------------
info "Setting up Python virtual environment..."

if [ -d "$VENV_DIR" ]; then
    warn "Existing venv found at $VENV_DIR — reusing it."
else
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Created venv at $VENV_DIR"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# -----------------------------------------------------------------------
# 4. Install dependencies
# -----------------------------------------------------------------------
info "Installing Python dependencies..."

pip install --upgrade pip --quiet
pip install -r "$REQUIREMENTS" --quiet

ok "All dependencies installed."

# -----------------------------------------------------------------------
# 5. Verify key imports
# -----------------------------------------------------------------------
info "Verifying imports..."

"$PYTHON" -c "
import langgraph, langchain_openai, openai, psutil, tiktoken, rich, httpx
print('All imports OK')
" 2>/dev/null && ok "All packages import correctly." \
             || warn "Some imports failed — check the output above."

# pynvml is optional (only needed for NVIDIA GPU detection)
"$PYTHON" -c "import pynvml" 2>/dev/null \
    && ok "pynvml available (NVIDIA GPU detection enabled)" \
    || warn "pynvml not available — NVIDIA GPU detection will be skipped. (This is fine if you don't have an NVIDIA GPU.)"

# -----------------------------------------------------------------------
# 6. Summary
# -----------------------------------------------------------------------
echo
echo -e "${BOLD}-------------------------------------------------------${NC}"
echo -e "${GREEN}${BOLD}  Ora OS installation complete.${NC}"
echo -e "${BOLD}-------------------------------------------------------${NC}"
echo
echo -e "  ${BOLD}To start Ora OS:${NC}"
echo
echo -e "    source $VENV_DIR/bin/activate"
echo -e "    python main.py"
echo
echo -e "  ${BOLD}Before first run, make sure you have:${NC}"
echo -e "    1. Ollama running          ${CYAN}ollama serve${NC}"
echo -e "    2. At least one model      ${CYAN}ollama pull qwen3:4b${NC}"
echo
echo -e "  On first launch, a setup wizard will guide you through"
echo -e "  hardware detection, model configuration, and user profile."
echo
echo -e "  ${BOLD}Specs & docs:${NC}  specs/   README.md"
echo -e "${BOLD}-------------------------------------------------------${NC}"
