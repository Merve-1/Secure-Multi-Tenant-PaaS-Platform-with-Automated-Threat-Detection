#!/usr/bin/env bash
# Build and deploy the stack, then create the test user.
# Usage: TEST_EMAIL=you@example.com ./scripts/up.sh
set -euo pipefail
STACK=sam-fastapi
REGION=eu-north-1
cd "$(dirname "$0")/.."

aws sts get-caller-identity > /dev/null   # fail early if credentials are invalid

sam validate --lint
sam build
sam deploy --no-confirm-changeset --no-fail-on-empty-changeset

get_output() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
POOL_ID=$(get_output UserPoolId)
CLIENT_ID=$(get_output UserPoolClientId)
API_URL=$(get_output ApiUrl)

if [ -n "${TEST_EMAIL:-}" ]; then
  read -rsp "Password for $TEST_EMAIL (12+ chars, mixed case, number, symbol): " PASS
  echo
  aws cognito-idp admin-create-user --region "$REGION" \
    --user-pool-id "$POOL_ID" --username "$TEST_EMAIL" \
    --user-attributes Name=email,Value="$TEST_EMAIL" Name=email_verified,Value=true Name=custom:tenant_id,Value=tenant-001 \
    --message-action SUPPRESS > /dev/null
  aws cognito-idp admin-set-user-password --region "$REGION" \
    --user-pool-id "$POOL_ID" --username "$TEST_EMAIL" --password "$PASS" --permanent
  aws cognito-idp admin-add-user-to-group --region "$REGION" \
    --user-pool-id "$POOL_ID" --username "$TEST_EMAIL" --group-name admin
  echo "Test user created in group admin (tenant-001)."
fi

echo
echo "POOL_ID=$POOL_ID"
echo "CLIENT_ID=$CLIENT_ID"
echo "API_URL=$API_URL"
