#!/usr/bin/env python3
"""Fill in the API deployment hash.

API Gateway only serves a change after a new Deployment is created.
CloudFormation creates one when the Deployment's logical ID changes, so the
placeholder RENDERHASH in infrastructure/api-stack.yaml is replaced with a hash
of the template. Same template, same ID, no redeploy; any change, new ID.

    python scripts/render_api_template.py  ->  build/templates/api-stack.yaml
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "RENDERHASH"


def render(source=ROOT / "infrastructure" / "api-stack.yaml", target=ROOT / "build" / "templates" / "api-stack.yaml"):
    text = source.read_text()
    if PLACEHOLDER not in text:
        raise SystemExit(f"{PLACEHOLDER} not found in {source}")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(PLACEHOLDER, digest))
    print(f"{target.relative_to(ROOT)} (deployment ApiDeployment{digest})")
    return target


if __name__ == "__main__":
    render()
