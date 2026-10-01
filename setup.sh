#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
printf '\nInstalled. Run .venv/bin/dotbot setup, then .venv/bin/dotbot serve --local.\n'
