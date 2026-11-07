#!/usr/bin/env bash
# Make sure the private artifacts bucket exists (Lambda zips, the web app
# package and CloudFormation templates). Creates it on the first run in a new
# account; afterwards it only checks. The bucket is not in a stack, so deleting
# a stack never deletes the packages another stack is running from.
set -euo pipefail

: "${ARTIFACT_BUCKET:?}" "${AWS_REGION:?}"

if aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" 2>/dev/null; then
  echo "artifacts bucket exists: ${ARTIFACT_BUCKET}"
  exit 0
fi

echo "creating artifacts bucket ${ARTIFACT_BUCKET}"
if [[ "$AWS_REGION" == us-east-1 ]]; then
  aws s3api create-bucket --bucket "$ARTIFACT_BUCKET"
else
  aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}"
fi
aws s3api wait bucket-exists --bucket "$ARTIFACT_BUCKET"
aws s3api put-public-access-block --bucket "$ARTIFACT_BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$ARTIFACT_BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-tagging --bucket "$ARTIFACT_BUCKET" \
  --tagging 'TagSet=[{Key=Project,Value=MediSlot}]'
