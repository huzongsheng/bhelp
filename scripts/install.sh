#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT="$PROJECT_DIR/dist/bhelp"
INSTALL_DIR="${BHELP_INSTALL_DIR:-${HOME}/.local/bin}"
BHELP_PYTHON="${BHELP_PYTHON:-python3}"

if ! command -v "$BHELP_PYTHON" >/dev/null 2>&1; then
  printf '%s\n' "bhelp requires Python 3.11 or newer; '$BHELP_PYTHON' was not found." >&2
  exit 1
fi

"$BHELP_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "bhelp requires Python 3.11 or newer")'

mkdir -p "$PROJECT_DIR/dist" "$INSTALL_DIR"
"$BHELP_PYTHON" "$PROJECT_DIR/scripts/build_zipapp.py" --source "$PROJECT_DIR/src" --output "$OUTPUT"
install -m 0755 "$OUTPUT" "$INSTALL_DIR/bhelp"

printf '%s\n' "Installed bhelp to $INSTALL_DIR/bhelp"
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *) printf '%s\n' "Add $INSTALL_DIR to PATH before using bhelp." ;;
esac
