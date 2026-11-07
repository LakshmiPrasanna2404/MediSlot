#!/usr/bin/env bash
# Lint: Python (ruff), YAML (yamllint), CloudFormation (cfn-lint on every stack,
# including the rendered API template).
set -euo pipefail
python scripts/render_api_template.py
ruff check .
yamllint -c .yamllint.yml .github infrastructure .checkov.yaml .yamllint.yml
cfn-lint --non-zero-exit-code error infrastructure/*-stack.yaml build/templates/api-stack.yaml
