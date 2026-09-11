#!/bin/sh
set -eu
T2MA_PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$T2MA_PROJECT"
uv sync --frozen --extra voice
uv run t2ma models
uv run t2ma doctor
printf '\nFor live calls, install the virtual drivers in your Terminal:\n  brew install --cask blackhole-2ch blackhole-16ch\nSee docs/SETUP.md for routing.\n'
