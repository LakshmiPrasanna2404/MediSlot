#!/usr/bin/env bash
# IaC scan (checkov). Blocks on any finding not justified in .checkov.yaml or inline.
set -euo pipefail
python scripts/render_api_template.py
rm -rf build/scan && mkdir -p build/scan
cp infrastructure/*-stack.yaml build/scan/
cp build/templates/api-stack.yaml build/scan/api-stack.yaml
checkov -d build/scan --config-file .checkov.yaml
