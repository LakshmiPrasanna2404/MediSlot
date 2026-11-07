# CI/CD

Two GitHub Actions workflows. `main` is protected, so all work arrives through pull requests.

## Pull request: `.github/workflows/ci.yml`

```
Lint ─────────┬─▶ Unit tests ─┐
              │               ├─▶ Integration tests
Security scans ───────────────┘
```

| Job (required check) | What it runs | Fails when |
|---|---|---|
| **Lint** | `scripts/ci/lint.sh`: renders the API template, then ruff (Python), yamllint (workflows, templates, tool config), cfn-lint (every template) | Any ruff or yamllint finding; any cfn-lint error (the templates currently have no cfn-lint warnings either) |
| **Security scans** | gitleaks over every commit in the PR; `scripts/ci/iac_scan.sh` (checkov on every template, with skips justified in `.checkov.yaml` or inline) | A secret in any commit; a failed checkov check |
| **Unit tests** | pytest `tests/unit` against a real MySQL 8.4 service container; AWS calls go to moto, with tables built from `data-stack.yaml` itself | Any failure |
| **Integration tests** | pytest `tests/integration -m integration` against the deployed dev API, using `AWS_TEST_ROLE_ARN` (read-only plus invoking the dev slot materialiser) | Any failure |

The unit tests cover:

- migrations on a fresh database and on the old schema;
- auth: password policy, bcrypt cost, token tampering, `alg: none`, expiry, rotation, logout;
- every authorizer rule;
- slot generation across DST changes, idempotency and sync;
- every doctor and patient endpoint, including role isolation;
- all 16 search filter combinations, with an `EXPLAIN` check;
- the Flask web app.

Integration tests run against what is currently deployed from `main`. They prove the PR has not broken anything
that is already live, and the same suite runs again after the PR's own deployment (below). Every test account uses
a unique email, so runs never collide.

`scripts/github/protect_main.sh <owner/repo>` sets the branch protection: PR required, one approving review, stale
approvals dismissed, the four checks above required and up to date with `main`, admins included, no force push,
no deletion.

## Merge to main: `.github/workflows/deploy.yml`

```
Lint ┐
Security scans ┼─▶ Package and upload ─▶ Deploy stacks (change sets) ─▶ Database migrations ─▶ Integration tests ─▶ Smoke test
Unit tests ┘
```

1. **Package and upload.**
   - Reads the artifacts bucket and the CloudFormation execution role from the `medislot-bootstrap` stack.
   - Downloads the RDS CA bundle (Lambdas verify TLS to MySQL).
   - `scripts/build_lambdas.py` builds one zip per function: the handler, the shared `medislot_common` package,
     the pinned Linux wheels and the CA bundle. It also builds the web app package.
   - Zips are reproducible (fixed timestamps and order), and the S3 key is the content hash. An unchanged function
     keeps its key, so CloudFormation does not update it.
   - `scripts/render_api_template.py` puts a hash of the API template into the deployment's logical ID, so every
     API change creates a new deployment.
2. **Deploy stacks.**
   - Preflight checks: the seeded SecureStrings exist, and a warning if RDS is still on MySQL 8.0.
   - Then every stack in order (network, data, config, iam, auth, app, jobs, monitoring, dashboard, api) through
     `scripts/cfn_deploy.py`.
   - Each update is a **change set**: created, every change printed to the run log (action, resource, whether it is
     a replacement), executed, waited for.
   - A change set with no changes is deleted and the step passes.
   - Resources that were created by hand before they were in a template are brought in once with an IMPORT change
     set (`--import`).
   - Nothing is updated outside a change set.
3. **Database migrations.**
   - Invokes `medislot-dev-db-migrate` with `check`, which proves a Lambda in the private subnets reaches RDS over
     TLS.
   - Then invokes it with `migrate`, which applies pending files from `database/migrations/` in order, under a MySQL
     named lock, and records each one in `schema_migrations`.
4. **Integration tests.** The same suite as on the PR, now against the new deployment.
5. **Smoke test.**
   - Bad login returns 401.
   - `/doctors` without a token returns 401.
   - The web app home page answers.

Only one deployment runs at a time (`concurrency: deploy-dev`), and a running deployment is never cancelled.

**Rollback.** A failed change set rolls its stack back automatically, and the job stops; later stacks are not
touched. To undo a merged change, revert the PR. The revert goes through the same pipeline. Migrations only move
forward. A migration that must be undone is followed by a new migration.

## Secrets strategy

| Where | Name | Holds | Notes |
|---|---|---|---|
| GitHub repository secret | `AWS_ROLE_ARN` | `GitHubDeployRoleArn` output of the bootstrap stack | Not a credential: a role GitHub can assume only from `main` through OIDC |
| GitHub repository secret | `AWS_TEST_ROLE_ARN` | `GitHubTestRoleArn` output | Assumable from pull requests and `main`; read-only plus one invoke |
| GitHub repository variable | `AWS_REGION` | e.g. `us-east-1` | |
| GitHub repository variable | `ALARM_EMAIL` | Email for the alarm SNS topic | Confirm the subscription email once |
| SSM SecureString | `/medislot/<env>/auth/jwt-secret`, `/db/password`, `/dashboard/session-secret` | Generated by `scripts/seed_secrets.sh` | Never in GitHub, in the repo or in an environment variable |
| SSM String | `/medislot/<env>/db/username`, `/medislot/<env>/config/*`, `/medislot/<env>/auth/*` | Settings | Config and auth parameters come from stacks |

- **No long-lived AWS keys anywhere.** GitHub gets short-lived credentials through OIDC. The deploy role trusts only
  `repo:<owner>/<repo>:ref:refs/heads/main`.
- The deploy role cannot change IAM itself. It passes the CloudFormation execution role, and only CloudFormation acts
  with that role, through change sets on `medislot-*` stacks.
- Lambdas read secrets from SSM at cold start, with their own role scoped to the parameter paths they need.
- gitleaks blocks any PR that adds a secret, in any commit, and `.gitignore` excludes `.env`, `build/` and local
  state.
