#!/usr/bin/env bash
# IaC scan (checkov), run by hand; not part of the pipeline. Findings that are
# accepted carry an inline skip with the reason in the template.
set -euo pipefail
checkov -d infrastructure --framework cloudformation
