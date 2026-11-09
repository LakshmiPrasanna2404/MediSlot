# Runbook

Commands assume the repository root, the AWS CLI v2 signed in to the account, and the GitHub CLI (`gh`) signed in
as a repository admin. `dev` is the environment unless stated otherwise.

## 1. What the pipeline needs, and what it creates itself

There is one workflow, `.github/workflows/deploy.yaml` (**MediSlot CloudFormation CI/CD Pipeline**).

**Set once by hand:**

- The GitHub OIDC role in AWS, `medislot-github-actions-role`. It is the same role the pipeline already used.
- The repository settings that point the pipeline at that role:

| GitHub setting | Value |
|---|---|
| Secret `AWS_ROLE_ARN` | ARN of `medislot-github-actions-role` |
| Variable `AWS_REGION` | e.g. `us-east-1` |
| Variable `ALARM_EMAIL` (optional) | Address for alarm emails; empty means no subscriber |

**Created by the pipeline on its first run, and only checked afterwards:**

- **The artifacts bucket** `medislot-<env>-<account>-<region>-artifacts`, which holds the Lambda zips, the web app
  package and the templates (`scripts/ci/ensure_artifact_bucket.sh`).
- **The values in SSM** (`scripts/seed_secrets.sh`): the JWT signing key and the database username and password.
  Random values are created only if missing, are never printed and are never overwritten. The web app's session key
  is generated on the instance itself at first boot.
- **The CloudFormation execution role** `medislot-<env>-cloudformation-execution-role`, in the iam stack. network,
  data and iam are deployed with the pipeline's own role. Every other stack goes through this role.

## 2. Deploying

- **Pull request into main:** Lint, Security scans and Unit tests run. These are the required checks.
- **Merge, or Actions → MediSlot CloudFormation CI/CD Pipeline → Run workflow:** the same checks, then one job per
  stack, then the database migrations, the integration tests against the deployed API and a smoke test.

| Order | Job | Stack | Deployed with |
|---|---|---|---|
| 1 | Prepare and Package | artifacts bucket, secrets, Lambda and web packages | pipeline role |
| 2 | Deploy Network Stack | `medislot-dev-network` | pipeline role |
| 3 | Deploy Data Stack | `medislot-dev-data` | pipeline role |
| 4 | Deploy IAM Stack | `medislot-dev-iam` | pipeline role |
| 5 | Deploy Config Stack | `medislot-dev-config` | execution role |
| 6 | Deploy Auth and Authorizer Lambdas | `medislot-dev-auth` | execution role |
| 7 | Deploy Doctor and Patient Lambdas | `medislot-dev-app` | execution role |
| 8 | Deploy Scheduled Jobs | `medislot-dev-jobs` | execution role |
| 9 | Deploy Monitoring and Alarms | `medislot-dev-monitoring` | execution role |
| 10 | Deploy Web App (EC2) | `medislot-dev-dashboard` | execution role |
| 11 | Deploy REST API | `medislot-dev-api` | execution role |
| 12 | Database Migrations | RDS schema (`database/migrations`) | pipeline role |
| 13 | Integration Tests, Smoke Test | — | pipeline role |

**How each stack is deployed.** Every stack is updated through a change set, and the log lists each change
(action, resource, whether it is replaced) before it runs.

**Safeguards in `scripts/cfn_deploy.py`:**

- **Existing resources are adopted.** If the template would create a table, bucket, database, log group, SSM
  parameter, Lambda function, IAM role or alarm whose name already exists in the account, it is imported into the
  stack first instead of failing.
- **Deployed parameter values are kept.** A parameter that is not passed keeps its deployed value, as
  `aws cloudformation deploy` does.
- **Rejected change sets are explained.** If CloudFormation's early validation rejects a change set, the job prints
  the resource and the reason.

- **Protected resources.** The database, every DynamoDB table, the photo bucket, the VPC and the subnets are
  protected. If an update would replace one of them, the job stops before anything changes and names the property
  that forces the replacement.
- **Leftovers from failed runs are recovered automatically:**
  - a `ROLLBACK_COMPLETE` stack is deleted and created again;
  - an `UPDATE_ROLLBACK_FAILED` stack has its rollback continued first.

**Credentials.** Each job assumes the role on its own, so no job runs on credentials older than one hour, the
role's maximum session.

## 3. The first run from the earlier setup

The first run updates the stacks your earlier pipeline created, in place, as follows.

**Data stack:**

- **Imports** the tables that were made by hand (refresh tokens, token revocations, idempotency) and the photo bucket
  into the stack. It uses an IMPORT change set, and the data in them is kept.
- **Leaves the RDS instance untouched.** Its definition is the same as before, including 1-day backups in dev.
- **Turns on point-in-time recovery** for the tables.

**IAM stack:**

- **Keeps** `medislot-dev-cloudformation-execution-role`. It is the service role of the auth, api, app, monitoring
  and dashboard stacks, and is given the permissions the new stacks need.
- **Adds** one role per Lambda function.
- **Removes** the unused report role.

**API and app stacks:**

- **Replaces the HTTP API with the REST API.** The API URL changes. The pipeline passes the new URL to the web app stack;
  anything else that saved the old URL must be updated.
- **Removes the single app Lambda and its routes,** and deploys the per-function Lambdas.

**Web app stack:**

- **Moves the instance to the doctor and patient portal.** CloudFormation stops and starts it to run the new
  boot script.
- **Keeps the Elastic IP,** so `DashboardUrl` does not change.
- **Opens port 80 to everyone,** as in the architecture.

**Migrations:**

- **V001** creates every table of the final schema that is missing. This fixes the missing `users` table.
- **V002** aligns tables that already existed in the old shape.
- **V003** adds the indexes.
- **V004** seeds the specialisations.

Each guarded step checks the current state first, so a step that has already been done is skipped.

**After the first green run:**

- In a later PR, remove the legacy `AppLambdaRole` from `iam-stack.yaml`. It is kept for one release because the old
  app stack still used it when this run started.
- Confirm the alarm email subscription, if `ALARM_EMAIL` is set.
- Open `DashboardUrl`, register a doctor, add consultation types and weekly availability, then register a patient
  and browse that doctor's slots:

  ```bash
  aws cloudformation describe-stacks --stack-name medislot-dev-dashboard --query "Stacks[0].Outputs" --output table
  aws cloudformation describe-stacks --stack-name medislot-dev-api --query "Stacks[0].Outputs" --output table
  ```

## 4. Everyday operations

| Task | How |
|---|---|
| Deploy a change | Branch, PR, review, merge. Never commit to `main`. |
| See what a deploy changed | Each Deploy … Stack job's log lists every change set entry (action, resource, replacement). |
| Rebuild one doctor's slots now | `aws lambda invoke --function-name medislot-dev-slot-materialiser --cli-binary-format raw-in-base64-out --payload '{"doctorId":"<id>"}' out.json && cat out.json` |
| Rebuild everyone's slots | the same with `--payload '{}'`; the result lists created, existing, removed and kept counts per doctor |
| Migration status | invoke `medislot-dev-db-migrate` with `{"action":"status"}` |
| Follow one request | Take `X-Correlation-Id` from the response or the web app's error page, then search it in CloudWatch Logs Insights across the `/aws/lambda/medislot-dev-*` groups: `fields @timestamp, level, message, @log \| filter correlationId = "<id>" \| sort @timestamp` |
| Find slow requests | API Gateway access logs (`/aws/apigateway/medislot-dev-api-access`) record `latencyMs` and `integrationLatencyMs` per `requestId`. The Lambda log lines for that request carry the same value as `apiRequestId`, next to the `correlationId`, and the Lambda `REPORT` line gives the duration. |
| Rotate the JWT secret | `aws ssm put-parameter --name /medislot/dev/auth/jwt-secret --type SecureString --value "$(openssl rand -base64 48)" --overwrite`. Lambdas pick it up on their next cold start; to force it, deploy any change to the auth stack. Every user has to sign in again. |
| Change a business rule (slot window, limits, page sizes, password policy) | Change the parameter default in the stack that uses it (`app-stack.yaml`, `jobs-stack.yaml` or `auth-stack.yaml`) in a PR. The deploy updates each function's `CONFIG_*` environment variables, and new containers use the new value. |
| Web app logs | EC2 → instance → Connect (Session Manager), then `journalctl -u medislot-web -n 200` |

## 5. Recovery

### A deploy job failed

CloudFormation rolls the stack back by itself, and the jobs after it do not run. The job log shows the failed
resources and the reason (`::error::` lines). Fix the cause in a PR and merge it; the next run carries on from
whatever state the stack is in:

- A stack whose first create failed (`ROLLBACK_COMPLETE`) is deleted and created again.
- A stack whose rollback failed (`UPDATE_ROLLBACK_FAILED`) has its rollback continued first. If the rollback
  cannot finish on its own, the job says so. Then, in the CloudFormation console, choose Stack actions → Continue
  update rollback, skip only the resource named in the error, and re-run the workflow.
- If an update would replace a protected resource (the database, a table, the photo bucket, the VPC or a subnet),
  the job stops before changing anything. It names the property that differs between the template and what is
  deployed. Make the template match the deployed value.

### Bad data after a migration

Migrations only move forward. Restore to a point in time from the RDS automated backups (kept 1 day in dev, 7 in prod) into
a new instance. Check the data, then plan the switch as its own change. DynamoDB tables have point-in-time recovery
for 35 days.

## 6. Planned maintenance

### Upgrade RDS MySQL 8.0 to 8.4

MySQL 8.0 left standard support on RDS on 31 July 2026. The deploy log shows a warning while the instance is still on
8.0. The template does not pin a version, so this is a one-off change outside CloudFormation and causes no drift:

```bash
aws rds describe-db-engine-versions --engine mysql --engine-version 8.4 \
  --query "DBEngineVersions[-1].EngineVersion" --output text            # latest 8.4.x
aws rds create-db-snapshot --db-instance-identifier medislot-dev-mysql --db-snapshot-identifier medislot-dev-pre-84
aws rds modify-db-instance --db-instance-identifier medislot-dev-mysql \
  --engine-version <8.4.x> --allow-major-version-upgrade --apply-immediately
```

Expect about 10 to 20 minutes of downtime. The unit tests already run on 8.4. Afterwards, invoke db-migrate with
`check`, and run the integration tests from the Actions tab.

### Encrypt the RDS storage

The existing dev instance was created unencrypted. It is a documented checkov exception, recorded in
`data-stack.yaml`. Encryption cannot be turned on in place. The instance keeps its name and therefore its endpoint,
so the stacks that import the endpoint are not affected:

1. PR: set `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain` on `MediSlotMySQL`. Merge and deploy.
2. Snapshot the instance, then make an encrypted copy:
   `aws rds copy-db-snapshot --source-db-snapshot-identifier <snap> --target-db-snapshot-identifier <snap>-enc --kms-key-id alias/aws/rds`.
3. PR: remove `MediSlotMySQL` from the template. Replace the `!GetAtt MediSlotMySQL...` and `!Ref MediSlotMySQL`
   outputs with their current literal values, so the exports do not change. Merge and deploy; the instance leaves
   the stack and keeps running.
4. Maintenance window:
   - Delete the instance (it was just snapshotted).
   - Restore `<snap>-enc` with the **same identifier**, the stack's DB subnet group and the RDS security group.
   - Wait until it is available.
5. PR: put `MediSlotMySQL` back with `StorageEncrypted: true` and the `!GetAtt` outputs. Add
   `--import MediSlotMySQL` to the Data step in `.github/workflows/deploy.yaml`. Merge; the pipeline imports the
   restored instance. In a follow-up PR, remove the `--import` flag and the checkov skip.

A new environment should start with `StorageEncrypted: true`.

## 7. Tear down an environment

The stacks deployed through the execution role are deleted first, while the iam stack (which owns that role) still
exists.

```bash
for s in api dashboard monitoring jobs app auth config iam data network; do
  aws cloudformation delete-stack --stack-name "medislot-dev-$s"
  aws cloudformation wait stack-delete-complete --stack-name "medislot-dev-$s"
done
```

The data stack leaves a final RDS snapshot and keeps the DynamoDB tables and the photo bucket (Retain). Delete
these, the SSM SecureStrings and the artifacts bucket by hand if they are no longer wanted.
