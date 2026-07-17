#!/bin/sh
# Uses only macOS built-in Python; does not create an AI environment.
set -eu
exec python3 "$(dirname "$0")/mac_index.py"
