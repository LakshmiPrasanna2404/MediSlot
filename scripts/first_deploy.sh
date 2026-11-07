#!/usr/bin/env bash
# The deploy pipeline's steps (.github/workflows/deploy.yml), run from an
# administrator's shell. For one situation only: the first deployment of a
# branch that introduces the pipeline itself, so that the pull request's
# integration tests have the new API to run against before it is merged
# (docs/runbook.md, section 2). Every later deployment goes through the
# pipeline.
#
#   bash scripts/first_deploy.sh dev
#
# Needs: AWS CLI v2 with AWS_REGION set, Python 3.12 with boto3 and cfn-flip,
# the medislot-bootstrap stack, and the secrets from scripts/seed_secrets.sh.
set -euo pipefail

ENVIRONMENT="${1:?usage: first_deploy.sh <dev|prod>}"
: "${AWS_REGION:?set AWS_REGION}"
cd "$(dirname "$0")/.."

bootstrap() {
  aws cloudformation describe-stacks --stack-name medislot-bootstrap \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}
BUCKET=$(bootstrap ArtifactsBucketName)
ROLE=$(bootstrap CloudFormationExecutionRoleArn)

for name in auth/jwt-secret db/username db/password dashboard/session-secret; do
  aws ssm get-parameter --name "/medislot/${ENVIRONMENT}/${name}" --query Parameter.Name --output text >/dev/null \
    || { echo "/medislot/${ENVIRONMENT}/${name} is missing: run scripts/seed_secrets.sh ${ENVIRONMENT}" >&2; exit 1; }
done

mkdir -p build
curl -sSfL -o build/rds-ca-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
python scripts/build_lambdas.py --env "$ENVIRONMENT" --ca-bundle build/rds-ca-bundle.pem --upload "$BUCKET"
python scripts/render_api_template.py

key() { python -c "import json,sys; m=json.load(open('build/manifest.json')); print(m['functions'][sys.argv[1]] if sys.argv[1] != 'dashboard' else m['dashboard'])" "$1"; }
prefix_list() {
  aws ec2 describe-managed-prefix-lists --filters "Name=prefix-list-name,Values=com.amazonaws.${AWS_REGION}.$1" \
    --query 'PrefixLists[0].PrefixListId' --output text
}

deploy() {
  local stack="$1" template="$2"; shift 2
  python scripts/cfn_deploy.py --stack "medislot-${ENVIRONMENT}-${stack}" --template "$template" \
    --role-arn "$ROLE" --bucket "$BUCKET" --tag "Environment=${ENVIRONMENT}" \
    --param "Environment=${ENVIRONMENT}" "$@"
}

deploy network infrastructure/network-stack.yaml \
  --param "S3PrefixListId=$(prefix_list s3)" --param "DynamoDBPrefixListId=$(prefix_list dynamodb)"
deploy data infrastructure/data-stack.yaml \
  --import RefreshTokensTable --import TokenRevocationsTable --import IdempotencyTable --import DoctorProfileFilesBucket
deploy config infrastructure/config-stack.yaml
deploy iam infrastructure/iam-stack.yaml
deploy auth infrastructure/auth-stack.yaml \
  --param "AuthCodeS3Bucket=$BUCKET" --param "AuthCodeS3Key=$(key auth)" \
  --param "AuthorizerCodeS3Bucket=$BUCKET" --param "AuthorizerCodeS3Key=$(key authorizer)"
deploy app infrastructure/app-stack.yaml --param "CodeS3Bucket=$BUCKET" \
  --param "DoctorProfileCodeS3Key=$(key doctor-profile)" \
  --param "PhotoUploadCodeS3Key=$(key photo-upload)" \
  --param "ConsultationTypesCodeS3Key=$(key consultation-types)" \
  --param "AvailabilityCodeS3Key=$(key availability)" \
  --param "ScheduleCodeS3Key=$(key schedule)" \
  --param "PatientProfileCodeS3Key=$(key patient-profile)" \
  --param "DoctorSearchCodeS3Key=$(key doctor-search)" \
  --param "SlotLookupCodeS3Key=$(key slot-lookup)" \
  --param "DbMigrateCodeS3Key=$(key db-migrate)"
deploy jobs infrastructure/jobs-stack.yaml --param "CodeS3Bucket=$BUCKET" \
  --param "SlotMaterialiserCodeS3Key=$(key slot-materialiser)"
deploy monitoring infrastructure/monitoring-stack.yaml --param "AlarmEmail=${ALARM_EMAIL:-}"
deploy dashboard infrastructure/dashboard-stack.yaml \
  --param "DashboardCodeS3Bucket=$BUCKET" --param "DashboardCodeS3Key=$(key dashboard)"
deploy api build/templates/api-stack.yaml

for action in check migrate; do
  aws lambda invoke --function-name "medislot-${ENVIRONMENT}-db-migrate" \
    --cli-binary-format raw-in-base64-out --payload "{\"action\":\"${action}\"}" \
    --cli-read-timeout 180 build/migrate-out.json > build/migrate-meta.json
  cat build/migrate-out.json; echo
  if grep -q FunctionError build/migrate-meta.json; then echo "db-migrate ${action} failed" >&2; exit 1; fi
done
echo "Deployed medislot-${ENVIRONMENT}. Next: MEDISLOT_ENV=${ENVIRONMENT} python -m pytest tests/integration -m integration"
