#!/bin/sh
# Start S2S from this repository's virtual environment on macOS. Double-click it in Finder.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
    echo "The virtual environment is missing. See README.md, \"Development\"."
    read -r _
    exit 1
fi
exec .venv/bin/python -m s2s.app
