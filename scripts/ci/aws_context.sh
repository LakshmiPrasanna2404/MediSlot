#!/usr/bin/env bash
# Resolve the names every deploy job needs and export them to later steps of
# the same job ($GITHUB_ENV). Values are worked out in each job rather than
# passed between jobs, because job outputs that contain the AWS account ID can
# be withheld by GitHub as possible secrets.
#
#   ARTIFACT_BUCKET   medislot-<env>-<account>-<region>-artifacts
#   EXECUTION_ROLE    CloudFormation execution role from the iam stack
#                     (empty until the iam stack exists)
set -euo pipefail

: "${ENVIRONMENT:?}" "${AWS_REGION:?}" "${GITHUB_ENV:?}"

account=$(aws sts get-caller-identity --query Account --output text)
bucket="medislot-${ENVIRONMENT}-${account}-${AWS_REGION}-artifacts"

role=$(aws cloudformation describe-stacks --stack-name "medislot-${ENVIRONMENT}-iam" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFormationExecutionRoleArn'].OutputValue | [0]" \
  --output text 2>/dev/null || true)
[[ "$role" == arn:* ]] || role=""

{
  echo "ARTIFACT_BUCKET=${bucket}"
  echo "EXECUTION_ROLE=${role}"
} >> "$GITHUB_ENV"
echo "artifacts bucket: ${bucket}"
echo "execution role:   ${role:-<not created yet>}"
