#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT="$PROJECT_DIR/dist/bhelp"
INSTALL_DIR="${HOME}/.local/bin"

mkdir -p "$PROJECT_DIR/dist" "$INSTALL_DIR"
python3 -m zipapp "$PROJECT_DIR/src" -m "bhelp.cli:entrypoint" -p "/usr/bin/env python3" -o "$OUTPUT"
chmod 0755 "$OUTPUT"
install -m 0755 "$OUTPUT" "$INSTALL_DIR/bhelp"

printf '%s\n' "Installed bhelp to $INSTALL_DIR/bhelp"
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *) printf '%s\n' "Add $INSTALL_DIR to PATH before using bhelp." ;;
esac
