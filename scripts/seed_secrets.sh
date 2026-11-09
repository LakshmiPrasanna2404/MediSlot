#!/usr/bin/env bash
# Create the SecureString parameters that CloudFormation cannot create (it can
# only read them). The pipeline runs this before every deployment; it only
# creates what is missing, so in an account that already has them it changes
# nothing. Can also be run by hand:
#
#   bash scripts/seed_secrets.sh dev
#
# Existing values are never overwritten. Values are random and are never
# printed. To rotate the JWT signing key later:
#   aws ssm put-parameter --name /medislot/<env>/auth/jwt-secret --type SecureString \
#       --value "$(openssl rand -base64 48)" --overwrite
# (every signed-in user has to log in again).
set -euo pipefail

ENVIRONMENT="${1:?usage: seed_secrets.sh <dev|prod>}"
PREFIX="/medislot/${ENVIRONMENT}"

exists() {
  aws ssm get-parameter --name "$1" --query Parameter.Name --output text >/dev/null 2>&1
}

put() {
  local name="$1" type="$2" value="$3"
  if exists "$name"; then
    echo "kept     $name"
  else
    aws ssm put-parameter --name "$name" --type "$type" --value "$value" \
      --tags "Key=Project,Value=MediSlot" "Key=Environment,Value=${ENVIRONMENT}" >/dev/null
    echo "created  $name ($type)"
  fi
}

# JWT signing secret: 48 random bytes, base64 (64 characters).
put "${PREFIX}/auth/jwt-secret" SecureString "$(openssl rand -base64 48 | tr -d '\n')"

# RDS master credentials (read by CloudFormation when the database is created).
put "${PREFIX}/db/username" String "medislot_admin"
put "${PREFIX}/db/password" SecureString "$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | head -c 32)"
