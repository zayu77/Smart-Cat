#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
sudo .venv/bin/python scripts/smart_scale_service.py "$@"
