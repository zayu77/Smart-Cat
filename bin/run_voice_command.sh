#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
sudo .venv/bin/python scripts/voice_accessibility.py "$@"
