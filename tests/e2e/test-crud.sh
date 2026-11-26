#!/usr/bin/env bash
# test-crud.sh - end-to-end checks for organization-scoped projects and tasks
# on the DEPLOYED API: validation, roles, and isolation between organizations.
#
# Needs two Cognito users that already exist in the pool:
#   TEST_EMAIL=a@example.com TEST_EMAIL_B=b@example.com ./tests/e2e/test-crud.sh
#
# Passwords are typed at a prompt and are never stored or printed.
# The script removes everything it creates.

set -uo pipefail
STACK=sam-fastapi
REGION=eu-north-1

: "${TEST_EMAIL:?Set TEST_EMAIL (user A)}"
: "${TEST_EMAIL_B:?Set TEST_EMAIL_B (user B)}"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
API_URL="$(out ApiUrl)"; API_URL="${API_URL%/}"
CLIENT_ID="$(out UserPoolClientId)"
if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
  echo "Could not read stack outputs. Is the stack deployed?"; exit 1
fi

sign_in() {
  local pass token
  read -rsp "Password for $1: " pass >&2; echo >&2
  token=$(aws cognito-idp initiate-auth --region "$REGION" --client-id "$CLIENT_ID" \
    --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters USERNAME="$1",PASSWORD="$pass" \
    --query AuthenticationResult.IdToken --output text 2>/dev/null)
  unset pass
  if [ -z "$token" ] || [ "$token" = "None" ]; then echo "Sign-in failed for $1" >&2; return 1; fi
  echo "$token"
}

TOKEN_A="$(sign_in "$TEST_EMAIL")" || exit 1
TOKEN_B="$(sign_in "$TEST_EMAIL_B")" || exit 1
AUTH_TOKEN="$TOKEN_A"

PASSED=0; FAILED=0; STATUS=""
BODY_FILE="$(mktemp)"; trap 'rm -f "$BODY_FILE"' EXIT

call() {  # call METHOD PATH [JSON_BODY] [auth: yes|no]
  local args=(-s -o "$BODY_FILE" -w '%{http_code}' -X "$1" "$API_URL$2")
  [ "${4:-yes}" = "yes" ] && args+=(-H "Authorization: Bearer $AUTH_TOKEN")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  STATUS="$(curl "${args[@]}")"
}
as_a() { AUTH_TOKEN="$TOKEN_A"; }
as_b() { AUTH_TOKEN="$TOKEN_B"; }

check() {  # check DESCRIPTION EXPECTED_STATUS
  if [ "$STATUS" = "$2" ]; then
    PASSED=$((PASSED + 1)); printf '  PASS  %s (%s)\n' "$1" "$STATUS"
  else
    FAILED=$((FAILED + 1))
    printf '  FAIL  %s (expected %s, got %s)\n        %s\n' "$1" "$2" "$STATUS" "$(head -c 300 "$BODY_FILE")"
  fi
}
field() { python3 -c "import json;print(json.load(open('$BODY_FILE')).get('$1',''))"; }

echo "API: $API_URL"
echo
echo "Setup: user A creates an organization"
as_a
call POST /orgs '{"name":"CRUD Test Org"}';           check "create organization" 201
ORG="$(field id)"
if [ -z "$ORG" ]; then echo "No organization ID returned; stopping."; exit 1; fi
P="/orgs/$ORG/projects"

echo
echo "Authentication"
call GET /health "" no;                               check "health is public" 200
call GET "$P" "" no;                                  check "no token is rejected" 401
AUTH_TOKEN="garbage"; call GET "$P";                  check "invalid token is rejected" 401
as_a

echo
echo "Projects (user A, admin)"
call POST "$P" '{"name":"Demo project","description":"first project"}'
check "create project" 201
PID="$(field id)"
if [ -z "$PID" ]; then echo "No project ID returned; stopping."; exit 1; fi
call GET "$P/$PID";                                   check "get project" 200
call GET "$P";                                        check "list projects" 200
call PATCH "$P/$PID" '{"name":"Renamed project"}';    check "update project" 200

echo
echo "Validation"
call POST "$P" '{"description":"no name"}';           check "missing name rejected" 422
call POST "$P" '{"name":""}';                         check "empty name rejected" 422
call POST "$P" '{"name":"x","tenant_id":"other"}';    check "client cannot set tenant_id" 422
call POST "$P" '{"name":"x","status":"archived"}';    check "client cannot set status on create" 422
call PATCH "$P/$PID" '{}';                            check "empty update rejected" 422
call PATCH "$P/$PID" '{"status":"deleted"}';          check "bad status rejected" 422
call GET "$P/not-a-uuid";                             check "malformed ID rejected" 422
call GET "$P?limit=1000";                             check "limit above 100 rejected" 422
call GET "$P/00000000-0000-4000-8000-000000000000";   check "unknown project is 404" 404

echo
echo "Tasks"
T="$P/$PID/tasks"
call POST "$T" '{"title":"First task","due_date":"2026-12-31"}'
check "create task" 201
TID="$(field id)"
call POST "$T" '{"title":"bad","status":"nope"}';     check "bad task status rejected" 422
call POST "$T" '{"title":"bad","due_date":"31-12-2026"}'; check "bad date rejected" 422
call POST "$P/00000000-0000-4000-8000-000000000000/tasks" '{"title":"orphan"}'
check "task in unknown project is 404" 404
call GET "$T";                                        check "list tasks" 200
call GET "$T/$TID";                                   check "get task" 200
call PATCH "$T/$TID" '{"status":"done"}';             check "update task" 200
call PATCH "$T/$TID" '{"due_date":null}';             check "clear due date" 200
call DELETE "$P/$PID";                                check "project with tasks cannot be deleted" 409

echo
echo "Roles"
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"reviewer\"}"
check "add user B as reviewer" 201
as_b
call GET "$P";                                        check "reviewer can read projects" 200
call GET "$T";                                        check "reviewer can read tasks" 200
call POST "$P" '{"name":"reviewer attempt"}';         check "reviewer cannot create project" 403
call PATCH "$P/$PID" '{"name":"hijack"}';             check "reviewer cannot update project" 403
call POST "$T" '{"title":"reviewer attempt"}';        check "reviewer cannot create task" 403
as_a
call GET "/orgs/$ORG/members";                        check "admin lists members" 200
SUB_B="$(python3 - "$BODY_FILE" "$TEST_EMAIL_B" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(next((m["user_id"] for m in data["items"] if m["email"] == sys.argv[2].lower()), ""))
PY
)"
call PATCH "/orgs/$ORG/members/$SUB_B" '{"role":"member"}'
check "admin promotes B to member" 200
as_b
call POST "$P" '{"name":"Member project"}';           check "member can create project" 201
PID_B="$(field id)"
call PATCH "$P/$PID_B" '{"description":"edited"}';    check "member can update project" 200
call DELETE "$P/$PID_B";                              check "member cannot delete project" 403
call DELETE "$T/$TID";                                check "member cannot delete task" 403
as_a
call DELETE "$P/$PID_B";                              check "admin deletes member's project" 204

echo
echo "Isolation between organizations"
call DELETE "/orgs/$ORG/members/$SUB_B";              check "admin removes B from the organization" 204
as_b
call GET "$P";                                        check "non-member cannot list projects" 404
call GET "$P/$PID";                                   check "non-member cannot read project" 404
call PATCH "$P/$PID" '{"name":"hijack"}';             check "non-member cannot update project" 404
call DELETE "$P/$PID";                                check "non-member cannot delete project" 404
call POST "$T" '{"title":"intruder"}';                check "non-member cannot create task" 404
call POST /orgs '{"name":"B Org"}';                   check "B creates own organization" 201
ORG2="$(field id)"
call GET "/orgs/$ORG2/projects/$PID";                 check "A's project ID is not found in B's org" 404
as_a
call GET "/orgs/$ORG2/projects";                      check "A cannot list B's organization" 404

echo
echo "Cleanup"
call DELETE "$T/$TID";                                check "delete task" 204
call DELETE "$P/$PID";                                check "delete project" 204
call GET "$P/$PID";                                   check "deleted project is gone" 404
call DELETE "/orgs/$ORG";                             check "delete organization" 204
as_b
call DELETE "/orgs/$ORG2";                            check "B deletes own organization" 204

echo
echo "Result: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]