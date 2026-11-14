#!/usr/bin/env bash
# Lint: Python (ruff), YAML (yamllint), CloudFormation (cfn-lint on every stack).
set -euo pipefail
ruff check .
yamllint -c .yamllint.yml .github infrastructure .yamllint.yml
cfn-lint --non-zero-exit-code error infrastructure/*-stack.yaml
