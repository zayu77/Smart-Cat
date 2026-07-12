#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
sudo .venv/bin/python scripts/mqtt_runtime_config.py \
  --mqtt-config config/mqtt.json \
  --output config/device_runtime.json \
  --policy-output config/device_policy.json \
  --optional \
  --timeout 3

sudo .venv/bin/python scripts/smart_scale_demo.py \
  --runtime-config config/device_runtime.json \
  --device-policy config/device_policy.json \
  "$@"
