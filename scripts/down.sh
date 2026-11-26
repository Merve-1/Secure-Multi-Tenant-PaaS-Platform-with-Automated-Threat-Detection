#!/usr/bin/env bash
# Delete the whole stack (Lambda, API, Cognito, log group).
set -euo pipefail
STACK=sam-fastapi
REGION=eu-north-1
cd "$(dirname "$0")/.."

echo "Deleting stack $STACK in $REGION..."
sam delete --stack-name "$STACK" --region "$REGION" --no-prompts
echo "Done. The stack and its Cognito users are gone."
