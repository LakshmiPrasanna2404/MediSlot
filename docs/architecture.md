# Architecture

The two diagrams below are the design this repository follows. Each numbered box maps to code and to a
CloudFormation stack in the tables that follow. The schema diagrams are covered in [data-model.md](data-model.md).

![Doctor architecture](MediSlot%20Doctor%20Architecture%20Diagram.png)

![Patient architecture](MediSlot%20Patient%20Architecture%20Diagram.png)

## Box by box

| Box | Component | Code | Stack | Status |
|---|---|---|---|---|
| 1 | Doctor and patient browsers | — | — | — |
| 2 | EC2, Flask web application (doctor and patient portals) | `dashboard/` | `dashboard-stack.yaml` | Deployed (doctor portal Milestone 3, patient portal Milestone 4) |
| 3 | API Gateway REST API, public and protected routes | OpenAPI body | `api-stack.yaml` | Deployed |
| 4 | Lambda Authorizer: JWT secret from SSM, HS256, expiry, jti denylist, role, context, cache TTL 0 | `lambda/authorizer/` | `auth-stack.yaml` | Deployed |
| 4a | Auth Lambda: register (bcrypt), login (JWT + refresh token), refresh, logout (jti to denylist) | `lambda/auth/` | `auth-stack.yaml` | Deployed |
| 5 (doctor) | Doctor Profile, Consultation Types, Availability (patterns and blackouts), Schedule, Photo Upload | `lambda/doctor_profile/`, `consultation_types/`, `availability/`, `schedule/`, `photo_upload/` | `app-stack.yaml` | Deployed |
| 5 (doctor) | Outcome Lambda, Slot Cancel Lambda | — | — | Milestones 5–6 (roles already in `iam-stack.yaml`) |
| 5 (patient) | Patient Profile, Doctor Search, Slot Lookup | `lambda/patient_profile/`, `doctor_search/`, `slot_lookup/` | `app-stack.yaml` | Deployed |
| 5 (patient) | Booking, Cancel, Reschedule Starter, Waitlist, Refund History | — | — | Milestones 5–6 (roles already in `iam-stack.yaml`) |
| 6 | RDS MySQL | `database/migrations/` (applied by `lambda/db_migrate/`) | `data-stack.yaml` | Deployed |
| 6 | DynamoDB: token revocation, refresh tokens, slots, seats, waitlist, hold offers, idempotency, dispatch log | `lambda/shared/medislot_common/` | `data-stack.yaml` | All 8 tables deployed; used from Milestones 2–3 onwards |
| 6 | S3 profile photos (pre-signed POST to upload, pre-signed GET to view) | `lambda/photo_upload/` | `data-stack.yaml` | Deployed |
| 6 | SSM Parameter Store: DB credentials, JWT secret, issuer and token lifetimes only. Other rules are stack parameters passed to each function as environment variables | `scripts/seed_secrets.sh` for SecureStrings | `auth-stack.yaml`; rules in `app-stack.yaml`, `jobs-stack.yaml`, `auth-stack.yaml` | Deployed |
| 7 | EventBridge Scheduler: Slot Materialiser, daily | `lambda/slot_materialiser/` | `jobs-stack.yaml` | Deployed |
| 7 | Offer Expiry, Reminder, No-show jobs | — | — | Milestones 5–6 |
| 8 | EventBridge bus, Waitlist Processor, Notification Lambda, SNS | — | — | Milestones 5–6 ([events.md](design/events.md)) |
| 9 | Step Functions: slot cancel (doctor), reschedule saga (patient) | — | — | Milestones 5–6 ([reschedule-saga.md](design/reschedule-saga.md)) |
| 10 | CloudWatch logs and metrics, alarms to SNS for the admin team | `medislot_common/logs.py` (JSON logs, EMF metrics) | — | Logs and metrics deployed; alarms and dashboards in Milestone 7 |
| 11 | VPC 10.0.0.0/16, public subnet for EC2, private subnets for Lambda and RDS, VPC endpoints, no NAT gateway | — | `network-stack.yaml` | Deployed |

## Request path

```
Browser ──HTTP :80──▶ EC2 Flask (server-side session holds the tokens)
                         │ HTTPS + Bearer JWT, X-Correlation-Id
                         ▼
                   API Gateway REST API ── public: register, login, refresh
                         │ protected
                         ▼
                   Lambda Authorizer (TTL 0) ──▶ SSM (secret, issuer), DynamoDB token-revocations
                         │ Allow + {userId, role, jti, exp}
                         ▼
                   Domain Lambda (private subnets) ──3306──▶ RDS MySQL
                                                  ──endpoint──▶ DynamoDB, S3, SSM
```

- The browser never holds a token. Flask keeps the access and refresh tokens in a server-side session (the cookie
  carries only a random session ID), sends them to the API, and refreshes once on a 401.
- Lambdas run in the two private Lambda subnets. With no NAT gateway, they reach AWS services only through VPC
  endpoints: DynamoDB and S3 gateway endpoints, and the SSM interface endpoint today. SNS, EventBridge and
  Step Functions endpoints are parameters in the network stack, turned on in the milestone that first uses them,
  because interface endpoints are billed per hour.
- RDS accepts 3306 only from the Lambda security group. The EC2 security group allows 80 from anywhere and has no
  path to RDS.
- Each Lambda has its own IAM role with only what it uses: table ARNs, SSM paths, the photo bucket prefix
  (`iam-stack.yaml`).

## Where the diagrams leave room for a decision

| Topic | Decision | Why |
|---|---|---|
| Doctor time zone | `doctors.timezone` column (IANA name, default `Asia/Kolkata`) | The brief requires slots generated in the doctor's local time and stored in UTC; the schema needs a place to keep the zone. The only addition to the RDS diagram. |
| Slot generation | Daily materialiser for a rolling 30-day window, plus a sync straight after each availability, blackout or time zone change | Changes show up in the slot browser at once; the daily run extends the window. Details in [timezone.md](design/timezone.md). |
| JWT issuer | Signed header parameter `iss`; payload has exactly sub, role, iat, exp, jti | Box 4 checks the issuer, and the payload claim set is fixed. See [auth.md](design/auth.md). |
| Authorizer cache | TTL 0 | Box 4 says so; logout must take effect at once. |
| EC2 private IP | Assigned by the subnet, not fixed at 10.0.1.10 | A fixed IP blocks CloudFormation from replacing the instance (the new one cannot start while the old one holds the address). The public address is an Elastic IP, so the URL stays the same. |
| Tracing | No X-Ray | Not part of the architecture. Requests are followed through the correlation ID in every log line (box 10, CloudWatch). |
| CloudWatch Logs endpoint | Off by default (`EnableCloudWatchLogsEndpoint`); an existing stack keeps its deployed value | Lambda ships its logs outside the VPC; nothing calls the Logs API from inside it. |
| Events to Lambda | EventBridge rules invoke Lambda directly, no SQS | As drawn in box 8. Retries and failure destinations are covered in [events.md](design/events.md). |
| Static files for the web app | Served by Flask from the instance | One small instance, low traffic; no extra S3 or CloudFront in the diagram. |
| EC2 instance type | t3.micro by default (t2.micro allowed by the parameter) | The diagram shows t2.micro, but AWS Free plan accounts cannot launch t2.micro. t3.micro is the free-plan equivalent and what the existing instance runs on. |
| Web server process | Flask's own threaded server under systemd | No separate WSGI server; one small instance with low traffic. |

## Stacks and deployment order

| Order | Stack | Contains | Deployed with |
|---|---|---|---|
| 1 | `medislot-<env>-network` | VPC, subnets, route tables, security groups, VPC endpoints | pipeline role |
| 2 | `medislot-<env>-data` | RDS, 8 DynamoDB tables, photo bucket | pipeline role |
| 3 | `medislot-<env>-iam` | Per-function roles, scheduler role, web app instance profile, CloudFormation execution role | pipeline role |
| 4 | `medislot-<env>-auth` | Auth and Authorizer Lambdas, JWT settings in SSM | execution role |
| 5 | `medislot-<env>-app` | Doctor and patient domain Lambdas, migration Lambda | execution role |
| 6 | `medislot-<env>-jobs` | Slot materialiser and its daily schedule | execution role |
| 7 | `medislot-<env>-dashboard` | EC2 web application and its Elastic IP | execution role |
| 8 | `medislot-<env>-api` | REST API, routes, models, gateway responses, stage | execution role |

All of it is deployed by one workflow, `.github/workflows/deploy.yaml`, one job per stack, each update through a
change set (`scripts/cfn_deploy.py`). The artifacts bucket and the SSM SecureStrings are created by the same
workflow when they are missing. The API stack is last so every Lambda it points to exists. The web app gets the
API URL from the API stack's `ApiUrl` output, which the pipeline passes to the dashboard stack.
