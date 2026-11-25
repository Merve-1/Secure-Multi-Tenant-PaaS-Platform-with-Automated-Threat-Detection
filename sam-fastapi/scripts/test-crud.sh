#!/usr/bin/env bash
# test-crud.sh - end-to-end checks against the DEPLOYED API.
#
# Usage:
#   TEST_EMAIL=you@example.com ./scripts/test-crud.sh


set -uo pipefail
STACK=sam-fastapi
REGION=eu-north-1

: "${TEST_EMAIL:?Set TEST_EMAIL to your test user, e.g. TEST_EMAIL=you@example.com}"


out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
API_URL="$(out ApiUrl)"; API_URL="${API_URL%/}"
CLIENT_ID="$(out UserPoolClientId)"
if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
  echo "Could not read stack outputs. Is the stack deployed?"; exit 1
fi

sign_in() {  # sign_in EMAIL -> prints an ID token
  local pass token
  read -rsp "Password for $1: " pass >&2; echo >&2
  token=$(aws cognito-idp initiate-auth --region "$REGION" --client-id "$CLIENT_ID" \
    --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters USERNAME="$1",PASSWORD="$pass" \
    --query AuthenticationResult.IdToken --output text 2>/dev/null)
  unset pass
  if [ -z "$token" ] || [ "$token" = "None" ]; then
    echo "Sign-in failed for $1" >&2; return 1
  fi
  echo "$token"
}

TOKEN="$(sign_in "$TEST_EMAIL")" || exit 1
AUTH_TOKEN="$TOKEN"

PASSED=0; FAILED=0
BODY_FILE="$(mktemp)"; trap 'rm -f "$BODY_FILE"' EXIT
STATUS=""

call() {  # call METHOD PATH [JSON_BODY] [auth: yes|no]
  local method="$1" path="$2" data="${3:-}" auth="${4:-yes}"
  local args=(-s -o "$BODY_FILE" -w '%{http_code}' -X "$method" "$API_URL$path")
  [ "$auth" = "yes" ] && args+=(-H "Authorization: Bearer $AUTH_TOKEN")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  STATUS="$(curl "${args[@]}")"
}

check() {  # check DESCRIPTION EXPECTED_STATUS
  if [ "$STATUS" = "$2" ]; then
    PASSED=$((PASSED + 1)); printf '  PASS  %s (%s)\n' "$1" "$STATUS"
  else
    FAILED=$((FAILED + 1))
    printf '  FAIL  %s (expected %s, got %s)\n        %s\n' "$1" "$2" "$STATUS" "$(head -c 300 "$BODY_FILE")"
  fi
}

check_absent() {  # check_absent DESCRIPTION TEXT  (body must NOT contain TEXT)
  if grep -q "$2" "$BODY_FILE"; then
    FAILED=$((FAILED + 1)); printf '  FAIL  %s\n' "$1"
  else
    PASSED=$((PASSED + 1)); printf '  PASS  %s\n' "$1"
  fi
}

field() {  # field NAME -> value of a top-level JSON field in the last response
  python3 -c "import json;print(json.load(open('$BODY_FILE')).get('$1',''))"
}

echo "API: $API_URL"
echo
echo "Authentication"
call GET /health "" no;            check "health is public" 200
call GET /projects "" no;          check "no token is rejected" 401
AUTH_TOKEN="garbage"; call GET /projects; check "invalid token is rejected" 401
AUTH_TOKEN="$TOKEN"

echo
echo "Projects"
call POST /projects '{"name":"Demo project","description":"first project"}'
check "create project" 201
PID="$(field id)"
if [ -z "$PID" ]; then echo "No project ID returned; stopping."; exit 1; fi
call GET "/projects/$PID";                         check "get project" 200
call GET /projects;                                check "list projects" 200
call PATCH "/projects/$PID" '{"name":"Renamed project"}'; check "update project" 200

echo
echo "Validation"
call POST /projects '{"description":"no name"}';           check "missing name rejected" 422
call POST /projects '{"name":""}';                         check "empty name rejected" 422
call POST /projects '{"name":"x","tenant_id":"other"}';    check "client cannot set tenant_id" 422
call POST /projects '{"name":"x","status":"archived"}';    check "client cannot set status on create" 422
call PATCH "/projects/$PID" '{}';                          check "empty update rejected" 422
call PATCH "/projects/$PID" '{"status":"deleted"}';        check "bad status rejected" 422
call GET /projects/not-a-uuid;                             check "malformed ID rejected" 422
call GET "/projects?limit=1000";                           check "limit above 100 rejected" 422
call GET /projects/00000000-0000-4000-8000-000000000000;   check "unknown project is 404" 404

echo
echo "Tasks"
call POST "/projects/$PID/tasks" '{"title":"First task","due_date":"2026-12-31"}'
check "create task" 201
TID="$(field id)"
call POST "/projects/$PID/tasks" '{"title":"bad","status":"nope"}';       check "bad task status rejected" 422
call POST "/projects/$PID/tasks" '{"title":"bad","due_date":"31-12-2026"}'; check "bad date rejected" 422
call POST "/projects/00000000-0000-4000-8000-000000000000/tasks" '{"title":"orphan"}'
check "task in unknown project is 404" 404
call GET "/projects/$PID/tasks";                    check "list tasks" 200
call GET "/projects/$PID/tasks/$TID";               check "get task" 200
call PATCH "/projects/$PID/tasks/$TID" '{"status":"done"}'; check "update task" 200
call PATCH "/projects/$PID/tasks/$TID" '{"due_date":null}'; check "clear due date" 200
call DELETE "/projects/$PID";                       check "project with tasks cannot be deleted" 409

# ------------------------------------------------------------ cross-tenant
if [ -n "${TEST_EMAIL_B:-}" ]; then
  echo
  echo "Cross-tenant isolation (user from another tenant)"
  TOKEN_B="$(sign_in "$TEST_EMAIL_B")" || exit 1
  AUTH_TOKEN="$TOKEN_B"
  call GET "/projects/$PID";                         check "other tenant cannot read project" 404
  call GET /projects;                                check "other tenant can list" 200
  check_absent "other tenant's list does not contain our project" "$PID"
  call GET "/projects/$PID/tasks/$TID";              check "other tenant cannot read task" 404
  call PATCH "/projects/$PID" '{"name":"hijacked"}'; check "other tenant cannot update project" 404
  call DELETE "/projects/$PID/tasks/$TID";           check "other tenant cannot delete task" 404
  AUTH_TOKEN="$TOKEN"
fi

# ------------------------------------------------------------------- roles
if [ -n "${TEST_EMAIL_VIEWER:-}" ]; then
  echo
  echo "Role check (viewer should be read-only)"
  TOKEN_V="$(sign_in "$TEST_EMAIL_VIEWER")" || exit 1
  AUTH_TOKEN="$TOKEN_V"
  call GET /projects;                                check "viewer can read" 200
  call POST /projects '{"name":"viewer attempt"}';   check "viewer cannot create" 403
  call DELETE "/projects/$PID/tasks/$TID";           check "viewer cannot delete" 403
  AUTH_TOKEN="$TOKEN"
fi

# ----------------------------------------------------------------- cleanup
echo
echo "Cleanup"
call DELETE "/projects/$PID/tasks/$TID"; check "delete task" 204
call DELETE "/projects/$PID";            check "delete project" 204
call GET "/projects/$PID";               check "deleted project is gone" 404

echo
echo "Result: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
