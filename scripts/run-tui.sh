#!/usr/bin/env bash
# Start the da-plus TUI with a UTF-8 locale, a 256-colour terminal and Python 3.11.
# Read-only launcher: it only sets environment variables and starts the TUI.
#
# Usage (as root on the server):
#   bash run-tui.sh [path-to-da-plus-folder] [--plain]
#
# Default project folder: /opt/da-plus-test/da-plus

set -euo pipefail

TUI_ARGS=()
PROJECT="/opt/da-plus-test/da-plus"
for arg in "$@"; do
    if [ "$arg" = "--plain" ]; then
        TUI_ARGS+=(--plain)
    else
        PROJECT="$arg"
    fi
done
PYTHON="/usr/bin/python3.11"

if [ ! -d "${PROJECT}/src/da_plus" ]; then
    echo "error: da-plus source not found in ${PROJECT} (pass the folder as the first argument)" >&2
    exit 1
fi

if [ ! -x "${PYTHON}" ]; then
    echo "error: ${PYTHON} not found" >&2
    exit 1
fi

if ! "${PYTHON}" -c "import textual, yaml" 2>/dev/null; then
    echo "error: textual or pyyaml is missing for ${PYTHON}; install them first (see README)" >&2
    exit 1
fi

# Use a UTF-8 locale if the default one is not UTF-8, so Chinese text and box characters render.
if ! locale charmap 2>/dev/null | grep -qi '^UTF-8$'; then
    export LANG=C.UTF-8
    export LC_ALL=C.UTF-8
fi

# A terminal type with enough colours for the TUI.
case "${TERM:-}" in
    linux|vt100|dumb) export DA_PLUS_TUI_PLAIN=1 ;;
    xterm|xterm-256color|screen-256color|tmux-256color) ;;
    *) export TERM=xterm-256color ;;
esac

cd "${PROJECT}"
exec env PYTHONPATH=src "${PYTHON}" -m da_plus tui "${TUI_ARGS[@]}"
