## What changed

<!-- One or two sentences. Link the milestone item. -->

## Why

## How it was tested

- [ ] `python -m pytest tests/unit` passes locally (or in CI)
- [ ] Integration tests / manual check against dev:

## What the reviewer should look at closely

<!-- Concurrency, IAM changes, migrations, anything hard to undo. -->

## Checklist

- [ ] Branch name follows `feature/`, `fix/`, `infra/` or `docs/`
- [ ] No credentials, `.env` files or build artefacts committed
- [ ] New thresholds and windows are SSM parameters, not literals in code
