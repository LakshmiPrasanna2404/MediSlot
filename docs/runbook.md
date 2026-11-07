# Runbook

Commands assume the repository root, AWS CLI v2 signed in to the target account with administrator rights (only the
one-time steps need them), the GitHub CLI (`gh`) signed in as a repository admin, Python 3.12 and `openssl`.
`ENV` is `dev` unless stated otherwise.

## 1. New AWS account, from nothing

### 1.1 Choose a region

```bash
export AWS_REGION=us-east-1        # any region with all the services in docs/architecture.md
```

### 1.2 Bootstrap stack (once per account, by hand)

It holds the GitHub OIDC trust, the deploy and test roles, the CloudFormation execution role and the artifacts
bucket. The pipeline cannot create these itself, because it needs them in order to run.

```bash
# Does the account already trust GitHub Actions?
aws iam list-open-id-connect-providers --query "OpenIDConnectProviderList[].Arn" --output text \
  | grep -q token.actions.githubusercontent.com && OIDC=false || OIDC=true

aws cloudformation deploy --stack-name medislot-bootstrap \
  --template-file infrastructure/bootstrap-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOwner=<github-user-or-org> GitHubRepo=MediSlot CreateOidcProvider=$OIDC

aws cloudformation describe-stacks --stack-name medislot-bootstrap \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
```

### 1.3 Seed the secrets (once per environment)

```bash
bash scripts/seed_secrets.sh dev
```

This creates the JWT signing secret, the database username and password, and the web app session key in SSM,
with random values that are never printed. It never overwrites a value that already exists.

### 1.4 GitHub settings

```bash
REPO=<owner>/MediSlot
gh secret set AWS_ROLE_ARN      --repo $REPO --body "<GitHubDeployRoleArn output>"
gh secret set AWS_TEST_ROLE_ARN --repo $REPO --body "<GitHubTestRoleArn output>"
gh variable set AWS_REGION      --repo $REPO --body "$AWS_REGION"
gh variable set ALARM_EMAIL     --repo $REPO --body "<team address for alarms>"
bash scripts/github/protect_main.sh $REPO
```

### 1.5 First deployment

Merge a pull request into `main`, or run the **Deploy** workflow by hand (Actions → Deploy → Run workflow). The run:

1. creates every stack in order;
2. applies the database migrations;
3. runs the integration and smoke tests.

The first run takes about 25 minutes, most of it RDS.

Afterwards:

- Confirm the SNS subscription email sent to `ALARM_EMAIL`.
- Note the addresses:

  ```bash
  aws cloudformation describe-stacks --stack-name medislot-dev-api --query "Stacks[0].Outputs" --output table
  aws cloudformation describe-stacks --stack-name medislot-dev-dashboard --query "Stacks[0].Outputs" --output table
  ```

- Open the `DashboardUrl`. Register a doctor, add consultation types and weekly availability, then register a
  patient and browse that doctor's slots.

## 2. One-time move from the earlier setup (the dev account as it was before this change)

The earlier pipeline used a hand-made OIDC role, a hand-made artifacts bucket and a CloudFormation execution role
in the IAM stack. Several tables and the photo bucket were made by hand. Moving over:

1. **Bootstrap stack.** Step 1.2 with `CreateOidcProvider=false`, because the provider already exists.
2. **Secrets.** Step 1.3. The database username, password and JWT secret already exist and are kept as they are
   (same names), so RDS and existing logins are not affected. Only the session key for the web app is new.
3. **GitHub.** Point `AWS_ROLE_ARN` at the new deploy role and add `AWS_TEST_ROLE_ARN`, `AWS_REGION` and
   `ALARM_EMAIL`. Run `protect_main.sh`.
4. **First deploy of the branch, from a shell.** The pull request that brings in the new pipeline cannot pass its
   own integration tests against the old deployment, and the new deploy role only trusts `main`. So once, before
   that PR is merged, run the pipeline's steps with administrator credentials:

   ```bash
   export AWS_REGION=<region> ALARM_EMAIL=<team address>
   git switch <the feature branch>
   pip install boto3==1.43.126 cfn-flip==1.3.0
   bash scripts/first_deploy.sh dev
   ```

   The deploy does the following by itself:

   - **Imports** the hand-made tables (refresh tokens, token revocations, idempotency) and the photo bucket into the
     data stack (an IMPORT change set; the log lists them). Their data is kept.
   - **Replaces the HTTP API** with the REST API. The API URL changes; the web app picks up the new one from SSM.
     Any API URL saved elsewhere must be updated.
   - **Removes** the earlier single app Lambda and its routes, and deploys the per-function Lambdas.
   - **Moves the web app instance** to the doctor and patient portal. The change set log shows whether the
     instance is updated or replaced. Either way the Elastic IP stays, so `DashboardUrl` does not change.
   - **Migrates the database.** V001 only creates tables that are missing (`CREATE TABLE IF NOT EXISTS`). V002
     aligns the existing tables with the final schema. V003 adds the indexes and V004 seeds the specialisations.
     Each guarded step checks the current state first, so a step that has already been done is skipped.

   Then re-run the PR's **Integration tests** check. It now tests the branch's own deployment. After review, merge.
   The pipeline on `main` redeploys the same packages, which changes nothing, and runs every test again.
5. **Clean up** after a green deploy:
   - Delete the old OIDC deploy role.
   - Delete the old artifacts bucket (empty it first).
   - In a later PR, remove the legacy `AppLambdaRole` from `iam-stack.yaml`. It is kept for one release because the
     old app stack was using it when the first deploy started.

## 3. Everyday operations

| Task | How |
|---|---|
| Deploy a change | Branch, PR, review, merge. Never commit to `main`. |
| See what a deploy changed | The Deploy run log lists every change set entry (action, resource, replacement). |
| Rebuild one doctor's slots now | `aws lambda invoke --function-name medislot-dev-slot-materialiser --cli-binary-format raw-in-base64-out --payload '{"doctorId":"<id>"}' out.json && cat out.json` |
| Rebuild everyone's slots | the same with `--payload '{}'`; the result lists created, existing, removed and kept counts per doctor |
| Migration status | invoke `medislot-dev-db-migrate` with `{"action":"status"}` |
| Follow one request | Take `X-Correlation-Id` from the response or the web app's error page, then search it in CloudWatch Logs Insights across the `/aws/lambda/medislot-dev-*` groups: `fields @timestamp, level, message, @log \| filter correlationId = "<id>" \| sort @timestamp` |
| Find slow requests | API Gateway access logs (`/aws/apigateway/medislot-dev-api-access`) record `latencyMs` and `integrationLatencyMs` per `requestId`. The Lambda log lines for that request carry the same value as `apiRequestId`, next to the `correlationId`, and the Lambda `REPORT` line gives the duration. |
| Rotate the JWT secret | `aws ssm put-parameter --name /medislot/dev/auth/jwt-secret --type SecureString --value "$(openssl rand -base64 48)" --overwrite`. Lambdas pick it up on their next cold start; to force it, deploy any change to the auth stack. Every user has to sign in again. |
| Change a business rule (refund tiers, windows, limits) | Edit `infrastructure/config-stack.yaml` in a PR. Lambdas cache config for the life of a container, at most a few minutes under steady traffic. |
| Web app logs | EC2 → instance → Connect (Session Manager), then `journalctl -u medislot-web -n 200` |

## 4. Recovery

### A stack is ROLLBACK_COMPLETE

A first create failed, and an empty stack is left that can only be deleted. The deploy role cannot delete stacks
on purpose, so this is done by hand:

```bash
aws cloudformation describe-stack-events --stack-name <stack> \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output table
aws cloudformation delete-stack --stack-name <stack> \
  --role-arn "$(aws cloudformation describe-stacks --stack-name medislot-bootstrap --query "Stacks[0].Outputs[?OutputKey=='CloudFormationExecutionRoleArn'].OutputValue | [0]" --output text)"
```

Fix the cause in a PR, then re-run the pipeline.

### An update failed

The stack rolls back by itself, and the pipeline stops before the next stack. Read the failed events (above), fix
the cause in a PR and merge it. If a stack is `UPDATE_ROLLBACK_FAILED`, continue the rollback in the console and
skip only the resource named in the error.

### Bad data after a migration

Migrations only move forward. Restore to a point in time from the RDS automated backups, which are kept 7 days, into
a new instance. Check the data, then plan the switch as its own change. DynamoDB tables have point-in-time recovery
for 35 days.

## 5. Planned maintenance

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
   `--import MediSlotMySQL` to the Data step in `deploy.yml`. Merge; the pipeline imports the restored instance.
   In a follow-up PR, remove the `--import` flag and the checkov skip.

A new environment should start with `StorageEncrypted: true`.

## 6. Tear down an environment

```bash
for s in api dashboard monitoring jobs app auth iam config; do
  aws cloudformation delete-stack --stack-name medislot-dev-$s
  aws cloudformation wait stack-delete-complete --stack-name medislot-dev-$s
done
aws cloudformation delete-stack --stack-name medislot-dev-data     # RDS leaves a final snapshot; tables and the photo bucket are retained
aws cloudformation wait stack-delete-complete --stack-name medislot-dev-data
aws cloudformation delete-stack --stack-name medislot-dev-network
```

Retained resources (DynamoDB tables, the photo bucket, the RDS final snapshot) and the SSM SecureStrings must be
deleted by hand if they are no longer wanted. Delete `medislot-bootstrap` last, after emptying the artifacts bucket.
