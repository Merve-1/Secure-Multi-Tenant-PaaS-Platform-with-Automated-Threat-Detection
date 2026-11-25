#!/usr/bin/env bash
# aws-overview.sh
# Shows (1) what exists in your AWS account and (2) what it has cost so far.
#
# Usage:
#   ./scripts/aws-overview.sh                 # resources + month-to-date cost
#   ./scripts/aws-overview.sh --costs         # cost only
#   ./scripts/aws-overview.sh --resources     # resources only
#
# Optional environment variables:
#   REGION=eu-north-1        region to scan (default: your configured region)
#   COST_START=2026-11-01    start date for the cost report (default: 1st of this month)
#
# Notes:
#   * Cost Explorer must be enabled in the account (Billing console, first time only).
#     Each Cost Explorer API request is billed at about $0.01; this script makes one.
#   * Cost data can lag by up to 24 hours, so it is an estimate, not a live meter.
#   * Serverless services (Lambda, API Gateway, Cognito) do not "run" like servers.
#     This script lists what exists, and flags the services that bill while idle.
#   * The script only reads. It never creates, changes or deletes anything.

set -uo pipefail
export AWS_PAGER=""

MODE="all"
case "${1:-}" in
  --costs)     MODE="costs" ;;
  --resources) MODE="resources" ;;
  "")          ;;
  *) echo "Usage: $0 [--costs|--resources]"; exit 1 ;;
esac

REGION="${REGION:-$(aws configure get region 2>/dev/null)}"
REGION="${REGION:-eu-north-1}"
COST_START="${COST_START:-$(date +%Y-%m-01)}"
COST_END="$(date -d tomorrow +%Y-%m-%d)"   # the end date is exclusive

section() { printf '\n== %s ==\n' "$1"; }

# Run an AWS command; print its output indented, "(none)" if empty,
# or a one-line note if the call failed (no access, service not enabled, etc.).
run() {
  local out
  if ! out=$("$@" 2>&1); then
    echo "  (not available: $(echo "$out" | head -n1 | cut -c1-110))"
    return
  fi
  if [ -z "$out" ] || [ "$out" = "None" ]; then
    echo "  (none)"
  else
    echo "$out" | sed 's/^/  /'
  fi
}

# Shorthand for regional calls, text output.
r() { aws "$@" --region "$REGION" --output text; }

# ---------------------------------------------------------------- identity
echo "Account / identity:"
run aws sts get-caller-identity --query '[Account,Arn]' --output text
echo "Region scanned: $REGION"

# ------------------------------------------------------------------- costs
show_costs() {
  section "COST from $COST_START to today (by service)"
  local raw agg unit
  if ! raw=$(aws ce get-cost-and-usage --region us-east-1 \
        --time-period Start="$COST_START",End="$COST_END" \
        --granularity MONTHLY --metrics UnblendedCost \
        --group-by Type=DIMENSION,Key=SERVICE \
        --query 'ResultsByTime[].Groups[].[Keys[0],Metrics.UnblendedCost.Amount,Metrics.UnblendedCost.Unit]' \
        --output text 2>&1); then
    echo "  (not available: $(echo "$raw" | head -n1 | cut -c1-150))"
    echo "  If Cost Explorer was never enabled, turn it on in the Billing console and"
    echo "  try again in about 24 hours."
    return
  fi
  if [ -z "$raw" ]; then
    echo "  (no cost data yet)"
    return
  fi
  unit=$(echo "$raw" | awk -F'\t' 'NR==1{print $3}')
  agg=$(echo "$raw" | awk -F'\t' '{a[$1]+=$2} END{for(k in a) printf "%.4f\t%s\n", a[k], k}' | sort -rn)
  echo "$agg" | awk -F'\t' -v u="$unit" '$1>=0.005{printf "  %9.2f %s  %s\n",$1,u,$2}'
  local hidden
  hidden=$(echo "$agg" | awk -F'\t' '$1<0.005{n++} END{print n+0}')
  [ "$hidden" -gt 0 ] && echo "  ($hidden services under 0.01 $unit not listed)"
  echo "$agg" | awk -F'\t' -v u="$unit" '{t+=$1} END{printf "  ---------\n  %9.2f %s  TOTAL\n",t,u}'
  echo "  Cost data can lag up to 24h. Credits and refunds may not be reflected here."
}

# --------------------------------------------------------------- resources
show_resources() {
  section "CloudFormation stacks"
  run r cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
      ROLLBACK_COMPLETE CREATE_FAILED ROLLBACK_FAILED UPDATE_FAILED DELETE_FAILED \
    --query 'StackSummaries[].[StackName,StackStatus]'

  section "Lambda functions (name, runtime, memory MB)"
  run r lambda list-functions --query 'Functions[].[FunctionName,Runtime,MemorySize]'

  section "API Gateway REST APIs (name, id)"
  run r apigateway get-rest-apis --query 'items[].[name,id]'

  section "API Gateway HTTP APIs (name, id)"
  run r apigatewayv2 get-apis --query 'Items[].[Name,ApiId]'

  section "Cognito user pools (name, id)"
  run r cognito-idp list-user-pools --max-results 60 --query 'UserPools[].[Name,Id]'

  section "DynamoDB tables"
  run r dynamodb list-tables --query 'TableNames[]'

  section "SNS topics"
  run r sns list-topics --query 'Topics[].[TopicArn]'

  section "EventBridge rules (name, state)"
  run r events list-rules --query 'Rules[].[Name,State]'

  section "S3 buckets (global)"
  run aws s3api list-buckets --query 'Buckets[].[Name]' --output text

  section "CloudFront distributions (global: id, domain, status)"
  run aws cloudfront list-distributions \
    --query 'DistributionList.Items[].[Id,DomainName,Status]' --output text

  section "CloudWatch log groups (name, retention days, bytes stored)"
  echo "  retention 'None' means the logs are kept forever"
  run r logs describe-log-groups \
    --query 'logGroups[].[logGroupName,retentionInDays,storedBytes]'

  echo
  echo "----------------------------------------------------------------"
  echo " WATCHLIST: services that bill while idle. Ideally all empty."
  echo "----------------------------------------------------------------"

  section "[!] EC2 instances running"
  run r ec2 describe-instances \
    --filters Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType]'

  section "[!] NAT gateways"
  run r ec2 describe-nat-gateways --filter Name=state,Values=available,pending \
    --query 'NatGateways[].[NatGatewayId,VpcId]'

  section "[!] Elastic IPs (public ip, association)"
  run r ec2 describe-addresses --query 'Addresses[].[PublicIp,AssociationId]'

  section "[!] Load balancers (name, type)"
  run r elbv2 describe-load-balancers --query 'LoadBalancers[].[LoadBalancerName,Type]'

  section "[!] RDS databases (id, class, status)"
  run r rds describe-db-instances \
    --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus]'

  section "[!] KMS customer-managed key aliases (about \$1/month each)"
  run r kms list-aliases \
    --query 'Aliases[?!starts_with(AliasName, `alias/aws/`)].[AliasName,TargetKeyId]'

  section "[!] Secrets Manager secrets (about \$0.40/month each)"
  run r secretsmanager list-secrets --query 'SecretList[].[Name]'

  section "[!] WAF web ACLs (regional)"
  run r wafv2 list-web-acls --scope REGIONAL --query 'WebACLs[].[Name]'

  section "[!] WAF web ACLs (CloudFront scope, us-east-1)"
  run aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
    --query 'WebACLs[].[Name]' --output text

  section "[!] GuardDuty detectors"
  run r guardduty list-detectors --query 'DetectorIds[]'

  section "[!] AWS Config recorders"
  run r configservice describe-configuration-recorders \
    --query 'ConfigurationRecorders[].[name]'

  section "[!] Security Hub"
  run r securityhub describe-hub --query 'HubArn'

  echo
  echo "Not covered: other regions, Route 53, Athena, Glue, ECS/EKS, Step Functions,"
  echo "and anything not listed above. Cost Explorer (above) covers every service."
}

case "$MODE" in
  costs)     show_costs ;;
  resources) show_resources ;;
  all)       show_resources; show_costs ;;
esac