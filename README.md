# MediSlot

Doctor appointment platform on AWS. Doctors publish weekly availability and patients find doctors and book slots.
Built with API Gateway, Lambda (Python 3.12), RDS MySQL, DynamoDB, S3, SSM Parameter Store, EventBridge
Scheduler and an EC2-hosted Flask web app. Everything is deployed with CloudFormation change sets from GitHub
Actions.

**Status: Milestones 1–4 complete.** Authentication, the doctor domain (profiles, consultation types,
availability, blackouts, slot generation, schedule) and the patient domain (profiles, doctor search, public doctor
profile, slot browser) are deployed. Booking, cancellation, refunds, rescheduling, the waitlist and notifications
follow in Milestones 5–6; their design is in `docs/design/`.

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The doctor and patient architecture diagrams, box by box, with the code and stack for each |
| [docs/data-model.md](docs/data-model.md) | RDS and DynamoDB schemas, constraints, every index with its query and `EXPLAIN` plan |
| [docs/api.md](docs/api.md) | Every endpoint: role, milestone, request, response, errors |
| [docs/design/auth.md](docs/design/auth.md) | JWT, refresh rotation, logout denylist, the authorizer and its cache TTL |
| [docs/design/timezone.md](docs/design/timezone.md) | Local availability, UTC slots, worked DST example |
| [docs/design/concurrency.md](docs/design/concurrency.md) | Booking under contention, idempotency, waitlist exclusivity |
| [docs/design/reschedule-saga.md](docs/design/reschedule-saga.md) | Reschedule steps and compensation |
| [docs/design/events.md](docs/design/events.md) | Event catalogue, notifications, scheduled jobs |
| [docs/cicd.md](docs/cicd.md) | Pipelines, required checks, secrets strategy |
| [docs/runbook.md](docs/runbook.md) | New account setup, move from the earlier setup, operations, recovery, teardown |
| [dashboard/README.md](dashboard/README.md) | The web app |

## Repository layout

```
.github/workflows/   ci.yml (pull requests), deploy.yml (merge to main)
dashboard/           Flask web app: doctor portal and patient portal (EC2)
database/migrations/ Versioned SQL migrations (applied by the db-migrate Lambda)
database/schema.sql  The resulting schema, for reference
docs/                Architecture, data model, API, design notes, CI/CD, runbook
infrastructure/      CloudFormation: bootstrap, network, data, config, iam, auth, app, jobs, monitoring, dashboard, api
lambda/<function>/   One folder per Lambda (handler + requirements.txt)
lambda/shared/       medislot_common: config, DB, HTTP, validation, tokens, slots, logging
scripts/             Build, deploy (change sets), secrets seeding, branch protection, CI helpers
tests/unit/          pytest: real MySQL, moto for AWS
tests/integration/   pytest against a deployed environment
```

## Working on it

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt checkov==3.2.459

# a local MySQL 8.4 for the unit tests (any of these works)
docker run -d --name medislot-mysql -p 3306:3306 -e MYSQL_ROOT_PASSWORD=root \
  -e MYSQL_USER=medislot -e MYSQL_PASSWORD=medislot-test-pw mysql:8.4
docker exec medislot-mysql mysql -uroot -proot -e "GRANT ALL ON *.* TO 'medislot'@'%'"

bash scripts/ci/lint.sh            # ruff, yamllint, cfn-lint
bash scripts/ci/iac_scan.sh        # checkov
python -m pytest                   # unit tests
MEDISLOT_ENV=dev python -m pytest tests/integration -m integration   # needs AWS credentials for the account
```

Unit tests read `MEDISLOT_TEST_DB_HOST`, `_PORT`, `_USER` and `_PASSWORD` (defaults: 127.0.0.1, 3306, medislot,
medislot-test-pw). They create and drop their own databases.

## How changes reach AWS

1. Branch from `main`: `feature/…`, `fix/…`, `infra/…` or `docs/…`.
2. Open a pull request using the template. CI runs lint, security scans, unit tests and integration tests; all four
   are required, and one approving review is required.
3. Merge. The deploy workflow packages the code, deploys every stack through change sets, runs migrations, then the
   integration and smoke tests.

First-time setup of an AWS account (bootstrap stack, secrets, GitHub settings) is in
[docs/runbook.md](docs/runbook.md).
