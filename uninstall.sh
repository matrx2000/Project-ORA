#!/usr/bin/env bash
# -----------------------------------------------------------------------
# O.R.A. — uninstall script
# Removes the Python venv, workspace data, and config pointer.
# Usage:  chmod +x uninstall.sh && ./uninstall.sh
# -----------------------------------------------------------------------
set -euo pipefail

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

cd "$(dirname "$0")"

VENV_DIR=".venv"

# -- resolve workspace and config paths (mirrors workspace_resolver.py) --
APP_NAME="ora-os"
APP_AUTHOR="OraOS"

if [[ "$OSTYPE" == "darwin"* ]]; then
    DATA_DIR="${HOME}/Library/Application Support/${APP_NAME}"
    CONFIG_DIR="${HOME}/Library/Application Support/${APP_NAME}"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    DATA_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/${APP_AUTHOR}/${APP_NAME}"
    CONFIG_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/${APP_AUTHOR}/${APP_NAME}"
else
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/${APP_NAME}"
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/${APP_NAME}"
fi

WORKSPACE_CONF="${CONFIG_DIR}/workspace.conf"

# If workspace.conf exists, read the actual workspace path from it
WORKSPACE_DIR="$DATA_DIR"
if [ -f "$WORKSPACE_CONF" ]; then
    SAVED=$(cat "$WORKSPACE_CONF" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$SAVED" ] && [ -d "$SAVED" ]; then
        WORKSPACE_DIR="$SAVED"
    fi
fi

# -- show what will be deleted -------------------------------------------
echo
echo -e "${RED}${BOLD}=======================================================${NC}"
echo -e "${RED}${BOLD}  O.R.A. — UNINSTALL${NC}"
echo -e "${RED}${BOLD}=======================================================${NC}"
echo
echo -e "${BOLD}This will permanently delete:${NC}"
echo
[ -d "$VENV_DIR" ] \
    && echo -e "  ${RED}1.${NC} Python venv        ${CYAN}$(pwd)/${VENV_DIR}${NC}" \
    || echo -e "  ${YELLOW}1.${NC} Python venv        (not found — skipping)"
[ -d "$WORKSPACE_DIR" ] \
    && echo -e "  ${RED}2.${NC} Workspace data     ${CYAN}${WORKSPACE_DIR}${NC}" \
    || echo -e "  ${YELLOW}2.${NC} Workspace data     (not found — skipping)"
[ -d "$CONFIG_DIR" ] \
    && echo -e "  ${RED}3.${NC} Config pointer     ${CYAN}${CONFIG_DIR}${NC}" \
    || echo -e "  ${YELLOW}3.${NC} Config pointer     (not found — skipping)"
echo
echo -e "${RED}${BOLD}  This includes all your settings, user profile, memory,${NC}"
echo -e "${RED}${BOLD}  session history, and model configurations.${NC}"
echo -e "${RED}${BOLD}  This action CANNOT be undone.${NC}"
echo

# -- require confirmation ------------------------------------------------
read -rp "  Type 'DELETE EVERYTHING' to confirm: " CONFIRM
echo

if [ "$CONFIRM" != "DELETE EVERYTHING" ]; then
    info "Uninstall cancelled. Nothing was deleted."
    exit 0
fi

# -- delete --------------------------------------------------------------
if [ -d "$VENV_DIR" ]; then
    info "Removing Python venv..."
    rm -rf "$VENV_DIR"
    ok "Removed ${VENV_DIR}"
fi

if [ -d "$WORKSPACE_DIR" ]; then
    info "Removing workspace data..."
    rm -rf "$WORKSPACE_DIR"
    ok "Removed ${WORKSPACE_DIR}"
fi

if [ -d "$CONFIG_DIR" ]; then
    info "Removing config directory..."
    rm -rf "$CONFIG_DIR"
    ok "Removed ${CONFIG_DIR}"
fi

echo
echo -e "${GREEN}${BOLD}  O.R.A. has been uninstalled.${NC}"
echo -e "  Project source code in ${CYAN}$(pwd)${NC} was kept."
echo -e "  To remove it too:  ${CYAN}rm -rf $(pwd)${NC}"
echo
