# CI/CD

One GitHub Actions workflow, `.github/workflows/deploy.yaml` (**MediSlot CloudFormation CI/CD Pipeline**).
`main` is protected, so all work arrives through pull requests.

## On a pull request into main

```
Lint ──▶ Unit tests
Security scans
```

| Job (required check) | What it runs | Fails when |
|---|---|---|
| **Lint** | `scripts/ci/lint.sh`: renders the API template, then ruff (Python), yamllint (workflow, templates, tool config), cfn-lint (every template) | Any ruff or yamllint finding; any cfn-lint error |
| **Security scans** | gitleaks over the commits; `scripts/ci/iac_scan.sh` (checkov on every template, skips justified in `.checkov.yaml` or inline) | A secret in any commit; a failed checkov check |
| **Unit tests** | pytest `tests/unit` against a real MySQL 8.4 service container; AWS calls go to moto, with tables built from `data-stack.yaml` itself | Any failure |

The unit tests cover:

- migrations on a fresh database and on the old schema;
- auth: password policy, bcrypt cost, token tampering, `alg: none`, expiry, rotation, logout;
- every authorizer rule;
- slot generation across DST changes, idempotency and sync;
- every doctor and patient endpoint, including role isolation;
- all 16 search filter combinations, with an `EXPLAIN` check;
- the Flask web app;
- the deploy script's recovery paths.

`scripts/github/protect_main.sh <owner/repo>` sets the branch protection:

- PR required, with one approving review; stale approvals are dismissed.
- The three checks above are required and must be up to date with `main`.
- Admins are included.
- No force push, no deletion.

## On a push to main (and Actions → Run workflow)

```
Lint, Security scans, Unit tests
  ▶ Prepare and Package
  ▶ Deploy Network Stack ▶ Deploy Data Stack ▶ Deploy IAM Stack
  ▶ Deploy Auth and Authorizer Lambdas ▶ Deploy Doctor and Patient Lambdas ▶ Deploy Scheduled Jobs
  ▶ Deploy Monitoring and Alarms ▶ Deploy Web App (EC2) ▶ Deploy REST API
  ▶ Database Migrations ▶ Integration Tests ▶ Smoke Test
```

1. **Prepare and Package**
   - Checks that `AWS_REGION` and `AWS_ROLE_ARN` are set.
   - Creates the artifacts bucket if it is missing.
   - Creates any missing SecureStrings (random values, never printed).
   - Warns if RDS is still on MySQL 8.0.
   - Downloads the RDS CA bundle. The Lambdas verify TLS to MySQL against it.
   - `scripts/build_lambdas.py` builds one zip per function: the handler, the shared `medislot_common` package, the
     pinned Linux wheels and the CA bundle. It also builds the web app package. The zips are reproducible, and the
     S3 key is the content hash, so an unchanged function keeps its key and is not updated.
   - `scripts/render_api_template.py` puts a hash of the API template into the deployment's logical ID, so every API
     change creates a new deployment.
2. **One job per stack**, each through `scripts/cfn_deploy.py`:
   - Every update is a **change set**: created, every change printed (action, resource, replacement), executed,
     waited for. A change set with no changes is deleted and the job passes.
   - Resources the template would create under a name that already exists in the account (created by hand, or
     left by an earlier stack) are adopted first with an IMPORT change set. This covers tables, buckets, the
     database, subnet groups, log groups, SSM parameters, Lambda functions, IAM roles and alarms.
   - A parameter that is not passed keeps the value the stack already has, as `aws cloudformation deploy` does, so
     a deploy never resets a setting to the template default.
   - If CloudFormation's early validation rejects a change set, the log names the resource and the reason.
   - If an update would replace the database, a table, the photo bucket, the VPC or a subnet (`--protect`), the
     job stops before anything changes.
   - Stacks left behind by failed runs are recovered first (`ROLLBACK_COMPLETE`, `REVIEW_IN_PROGRESS`,
     `UPDATE_ROLLBACK_FAILED`).
   - network, data and iam are deployed with the pipeline's role. Every other stack goes through
     `medislot-<env>-cloudformation-execution-role` from the iam stack.
3. **Database Migrations**
   - Invokes `medislot-<env>-db-migrate` with `check`, which proves a Lambda in the private subnets reaches RDS over
     TLS.
   - Then with `migrate`, which applies pending files from `database/migrations/` in order, under a MySQL named
     lock, and records each one in `schema_migrations`.
   - Then with `status`.
4. **Integration Tests**: `tests/integration` against the API that was just deployed.
5. **Smoke Test**
   - Bad login returns 401.
   - `/doctors` without a token returns 401.
   - The web app home page answers.

**Credentials.** Each job assumes the role on its own, so no job runs on credentials older than the role's
one-hour maximum session.

**Concurrency.** One deployment runs at a time per environment, and a running deployment is never cancelled. A
new push to a pull request cancels that PR's previous run.

**Rollback.** A failed change set rolls its stack back automatically, and the later jobs do not run. To undo a
merged change, revert the PR; the revert goes through the same pipeline. Migrations only move forward: a migration
that must be undone is followed by a new one.

## Secrets strategy

| Where | Name | Holds | Notes |
|---|---|---|---|
| GitHub repository secret | `AWS_ROLE_ARN` | ARN of the GitHub OIDC role | Not a credential: GitHub gets short-lived credentials for it through OIDC |
| GitHub repository variable | `AWS_REGION` | e.g. `us-east-1` | |
| GitHub repository variable | `ALARM_EMAIL` (optional) | Email for the alarm SNS topic | Confirm the subscription email once |
| SSM SecureString | `/medislot/<env>/auth/jwt-secret`, `/db/password` | Created by `scripts/seed_secrets.sh` (run by the pipeline) when missing | Never in GitHub, the repo, the logs or an environment variable |
| SSM String | `/medislot/<env>/db/username`; `/medislot/<env>/auth/jwt-issuer`, `access-token-seconds`, `refresh-token-seconds` | JWT settings | From the auth stack |
| Stack parameters | Slot window and lookup range, photo limits, page sizes, default doctor time zone, bcrypt work factor, password length | Business rules | Defaults in `app-stack.yaml`, `jobs-stack.yaml`, `auth-stack.yaml`; each function gets the ones it uses as `CONFIG_*` environment variables |
| On the web server | `/etc/medislot/session-secret` | Web session signing key | Generated on the instance at first boot, readable only by the app |

- **No long-lived AWS keys anywhere.**
- **SSM holds only the database credentials and the JWT values.** Lambdas read them at cold start, with their own
  role scoped to the parameter paths they need.
- **gitleaks blocks a pull request that adds a secret,** and `.gitignore` excludes `.env`, `build/` and local state.
