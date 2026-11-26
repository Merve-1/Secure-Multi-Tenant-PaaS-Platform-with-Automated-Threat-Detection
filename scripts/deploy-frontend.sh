#!/usr/bin/env bash
# Uploads ./frontend to the site bucket, writes config.json from the stack
# outputs (so no IDs are hard-coded), and refreshes CloudFront.
# Run from the project root after "sam deploy":  ./scripts/deploy-frontend.sh
set -euo pipefail
STACK=sam-fastapi
REGION=eu-north-1
cd "$(dirname "$0")/.."

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(out SiteBucketName); DIST=$(out DistributionId); API=$(out ApiUrl)
CLIENT=$(out UserPoolClientId); SITE=$(out SiteUrl)

if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Stack outputs missing. Add the frontend resources and run 'sam deploy' first."
  exit 1
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp frontend/* "$TMP"/
printf '{"apiUrl":"%s","clientId":"%s","region":"%s"}\n' "${API%/}" "$CLIENT" "$REGION" > "$TMP/config.json"

aws s3 sync "$TMP" "s3://$BUCKET" --delete --cache-control "no-cache" --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" > /dev/null
echo "Done. Open: $SITE"