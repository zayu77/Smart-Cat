#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
sudo .venv/bin/python scripts/voice_command_mqtt.py "$@"
