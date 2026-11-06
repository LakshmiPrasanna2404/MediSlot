# MediSlot operations dashboard

A read-only admin page on its own small EC2 instance. It shows API Gateway, Lambda,
DynamoDB and RDS health from CloudWatch, the state of the `medislot-{env}-*` alarms,
and recent errors from the auth and app Lambda logs. It never writes to anything.

Access is limited to one admin IP address (security group) plus a username and password.
The page is served over plain HTTP, so **the IP restriction is the real protection**.
Keep `DASHBOARD_ADMIN_CIDR` as narrow as you can (your public IP with `/32`).

## One-time setup (per environment)

The pipeline stops with a clear error if any of this is missing.

### 1. Create the login in SSM

Replace `dev` with `prod` for production. Run these from a shell where the AWS CLI is
signed in to the right account and region.

```bash
ENV=dev

# Username
aws ssm put-parameter --name /medislot/$ENV/dashboard/username \
  --type SecureString --value "admin"

# Password: this prints a bcrypt hash. Paste your password when asked.
# The password itself is never stored anywhere.
HASH=$(python -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass('Dashboard password: ').encode(), bcrypt.gensalt(12)).decode())")
aws ssm put-parameter --name /medislot/$ENV/dashboard/password-hash \
  --type SecureString --value "$HASH"

# Session signing key: 64 random hex characters
aws ssm put-parameter --name /medislot/$ENV/dashboard/session-secret \
  --type SecureString --value "$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

`pip install bcrypt` first if it is not installed. To change the password later, run the
second command again with `--overwrite`, then restart the service (see Troubleshooting).

### 2. Set GitHub repository variables

Settings > Secrets and variables > Actions > Variables:

| Variable | Required | Value |
|---|---|---|
| `DASHBOARD_ADMIN_CIDR` | yes | Your public IP with a prefix, for example `203.0.113.7/32` |
| `ALARM_EMAIL` | no | Address that receives alarm emails |

If your IP changes, update the variable and push (or run the workflow). CloudFormation
updates the security group in place.

### 3. Deploy, then confirm the alarm email

Push to `main`. The pipeline runs `deploy-monitoring` and `deploy-dashboard` after the app
stack and prints the dashboard URL. If you set `ALARM_EMAIL`, AWS sends a confirmation
email: click the link or the alarms will not reach you.

## Using it

Open the printed URL from the allowed IP and sign in. Pick Last hour / 6 hours / 24 hours;
everything on the page follows that range. Each chart has a **Table view** with the same
numbers. Charts update every minute, and CloudWatch results are cached for 2 minutes.

Things that look odd but are expected:

- **Authorizer errors** are rejected tokens. The authorizer raises "Unauthorized" for every
  bad token, so its Errors count includes normal 401 responses. It has no alarm.
- **Application metrics** shows "no data yet" until the booking, saga and waitlist code emits
  metrics in the `medislot` namespace. They appear here without any dashboard change.
- A missing count (no requests) is drawn as 0. A missing gauge (database CPU) is left as a gap.

## Demo: trigger one alarm

```bash
aws cloudwatch set-alarm-state --alarm-name medislot-dev-api-5xx \
  --state-value ALARM --state-reason "demo"
```

The dashboard shows it in red within about 2 minutes and the email arrives. CloudWatch
returns the alarm to OK on its next evaluation.

## Updating the dashboard

Every push builds a new package and passes its S3 key to the stack. That changes the
instance's boot script, so CloudFormation restarts the instance, and the boot script
downloads and installs the new package. Expect about a minute of downtime per deploy.

## Troubleshooting

The instance has no SSH. To read logs, use the EC2 console: Actions > Monitor and
troubleshoot > Get system log. The bootstrap script also logs there.

| Symptom | Likely cause |
|---|---|
| Page times out | Your IP is not `DASHBOARD_ADMIN_CIDR`, or the instance is still booting |
| "Dashboard is not configured" | An SSM parameter is missing or `session-secret` is under 32 characters |
| Panels say "unavailable" | The instance role lacks a CloudWatch permission (check the iam stack deployed) |
| Everything is empty or 0 | The API id or function names do not match this environment; check `/etc/medislot-dashboard.env` in the system log |
| Pipeline fails before deploy | A prerequisite above is missing; the error names it |

## Local preview and tests

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r dashboard/requirements.txt pytest
python -m pytest tests -q
```

`StaticCollector` in `dashboard/collectors.py` returns sample data, so the page can be
developed without AWS credentials.

## Cost

One `t3.micro` instance, an 8 GB volume, a public IPv4 address (about $3.60 a month) and the
CloudWatch alarms (the first 10 are free). Delete the dashboard stack when you are not
using it: `aws cloudformation delete-stack --stack-name medislot-dev-dashboard`.
