#!/usr/bin/env bash
# test-tasks.sh - checks isolated task operations on the DEPLOYED API:
# ownership rules, assignee validation, reviewer read-only, and isolation
# between organizations. Uses three existing Cognito users:
#
#   A = organization admin, B = member, C = member (later demoted to reviewer)
#
#   TEST_EMAIL=a@x.com TEST_EMAIL_B=b@x.com TEST_EMAIL_C=c@x.com ./tests/e2e/test-tasks.sh
#
# Passwords are typed at a prompt and are never stored or printed.
# The script removes everything it creates.

set -uo pipefail
STACK=sam-fastapi
REGION=eu-north-1

: "${TEST_EMAIL:?Set TEST_EMAIL (user A, admin)}"
: "${TEST_EMAIL_B:?Set TEST_EMAIL_B (user B, member)}"
: "${TEST_EMAIL_C:?Set TEST_EMAIL_C (user C, member)}"

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

sub_of() {
  python3 - "$1" <<'PY'
import sys, json, base64
p = sys.argv[1].split('.')[1]
p += '=' * (-len(p) % 4)
print(json.loads(base64.urlsafe_b64decode(p))['sub'])
PY
}

TOKEN_A="$(sign_in "$TEST_EMAIL")" || exit 1
TOKEN_B="$(sign_in "$TEST_EMAIL_B")" || exit 1
TOKEN_C="$(sign_in "$TEST_EMAIL_C")" || exit 1
SUB_B="$(sub_of "$TOKEN_B")"; SUB_C="$(sub_of "$TOKEN_C")"
AUTH_TOKEN="$TOKEN_A"

PASSED=0; FAILED=0; STATUS=""
BODY_FILE="$(mktemp)"; trap 'rm -f "$BODY_FILE"' EXIT

call() {  # call METHOD PATH [JSON_BODY]
  local args=(-s -o "$BODY_FILE" -w '%{http_code}' -X "$1" "$API_URL$2"
              -H "Authorization: Bearer $AUTH_TOKEN")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  STATUS="$(curl "${args[@]}")"
}
as_a() { AUTH_TOKEN="$TOKEN_A"; }
as_b() { AUTH_TOKEN="$TOKEN_B"; }
as_c() { AUTH_TOKEN="$TOKEN_C"; }

pass() { PASSED=$((PASSED + 1)); printf '  PASS  %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '  FAIL  %s\n        %s\n' "$1" "$(head -c 300 "$BODY_FILE")"; }
check() { if [ "$STATUS" = "$2" ]; then pass "$1 ($STATUS)"; else fail "$1 (expected $2, got $STATUS)"; fi; }
field() { python3 -c "import json;print(json.load(open('$BODY_FILE')).get('$1',''))"; }
check_field() { if [ "$(field "$2")" = "$3" ]; then pass "$1"; else fail "$1 (expected $2=$3, got $(field "$2"))"; fi; }

echo "API: $API_URL"
echo
echo "Setup"
as_a
call POST /orgs '{"name":"Task Isolation Org"}';                  check "A creates organization" 201
ORG="$(field id)"
if [ -z "$ORG" ]; then echo "No organization ID returned; stopping."; exit 1; fi
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"member\"}"
check "A adds B as member" 201
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_C\",\"role\":\"member\"}"
check "A adds C as member" 201
call POST "/orgs/$ORG/projects" '{"name":"Shared project"}';      check "A creates a project" 201
PID="$(field id)"
T="/orgs/$ORG/projects/$PID/tasks"

echo
echo "Members create their own tasks"
as_b
call POST "$T" '{"title":"B task"}';                              check "B creates a task" 201
T1="$(field id)"
check_field "owner defaults to the creator" owner "${TEST_EMAIL_B,,}"
check_field "creator can edit" can_edit True
as_c
call POST "$T" '{"title":"C task"}';                              check "C creates a task" 201
T2="$(field id)"

echo
echo "Ownership: members cannot touch other members' tasks"
as_b
call PATCH "$T/$T2" '{"status":"done"}';                          check "B cannot update C's task" 403
call DELETE "$T/$T2";                                             check "B cannot delete C's task" 403
call GET "$T/$T2";                                                check "B can still read C's task" 200
check_field "can_edit is false for B on C's task" can_edit False
check_field "can_delete is false for B on C's task" can_delete False
as_c
call PATCH "$T/$T1" '{"title":"hijack"}';                         check "C cannot update B's task" 403
as_b
call PATCH "$T/$T1" '{"status":"in_progress"}';                   check "B can update own task" 200

echo
echo "Admin and assignment"
as_a
call PATCH "$T/$T2" '{"title":"Renamed by admin"}';               check "admin can update any task" 200
call PATCH "$T/$T2" "{\"owner\":\"$TEST_EMAIL_B\"}";              check "admin assigns C's task to B" 200
call PATCH "$T/$T2" '{"owner":"nobody@example.com"}';             check "owner must be a member" 422
as_b
call PATCH "$T/$T2" '{"status":"done"}';                          check "assignee B can now update it" 200
call POST "$T" '{"title":"x","owner":"nobody@example.com"}';      check "cannot create a task for a non-member" 422
as_c
call PATCH "$T/$T2" '{"description":"creator edit"}';             check "creator C can still update it" 200

echo
echo "Validation"
as_b
call PATCH "$T/$T1" '{}';                                         check "empty update rejected" 422
call PATCH "$T/$T1" '{"created_by":"someone"}';                   check "cannot set created_by" 422
call PATCH "$T/$T1" '{"status":"nope"}';                          check "bad status rejected" 422
call POST "$T" '{"title":"x","tenant_id":"other"}';               check "cannot set tenant_id" 422

echo
echo "Reviewer is read-only (C demoted)"
as_a
call PATCH "/orgs/$ORG/members/$SUB_C" '{"role":"reviewer"}';     check "A demotes C to reviewer" 200
as_c
call GET "$T";                                                    check "reviewer can list tasks" 200
call GET "$T/$T2";                                                check "reviewer can read a task" 200
check_field "reviewer can_edit is false" can_edit False
call POST "$T" '{"title":"reviewer attempt"}';                    check "reviewer cannot create tasks" 403
call PATCH "$T/$T2" '{"status":"todo"}';                          check "reviewer cannot update, even as creator" 403
call DELETE "$T/$T2";                                             check "reviewer cannot delete" 403

echo
echo "Delete rules"
as_b
call DELETE "$T/$T1";                                             check "B deletes own task" 204
call DELETE "$T/$T1";                                             check "deleting again is 404" 404
as_a
call DELETE "$T/$T2";                                             check "admin deletes any task" 204

echo
echo "Isolation between organizations"
as_b
call POST /orgs '{"name":"B Org"}';                               check "B creates own organization" 201
ORG2="$(field id)"
call POST "/orgs/$ORG2/projects" '{"name":"B project"}';          check "B creates a project there" 201
PID2="$(field id)"
call POST "/orgs/$ORG2/projects/$PID2/tasks" '{"title":"B secret"}'
check "B creates a task there" 201
T3="$(field id)"
call GET "$T/$T3";                                                check "task of another org is not found via our org" 404
call PATCH "$T/$T3" '{"title":"hijack"}';                         check "cannot update it via our org" 404
call GET "/orgs/$ORG2/projects/$PID/tasks";                       check "our project ID does not exist in B's org" 404
as_a
call GET "/orgs/$ORG2/projects/$PID2/tasks";                      check "A is not a member of B's org" 404
call PATCH "/orgs/$ORG2/projects/$PID2/tasks/$T3" '{"title":"hijack"}'
check "A cannot update B's task" 404
call DELETE "/orgs/$ORG2/projects/$PID2/tasks/$T3";               check "A cannot delete B's task" 404

echo
echo "Cleanup"
as_b
call DELETE "/orgs/$ORG2/projects/$PID2/tasks/$T3";               check "B deletes own task" 204
call DELETE "/orgs/$ORG2/projects/$PID2";                         check "B deletes own project" 204
call DELETE "/orgs/$ORG2";                                        check "B deletes own organization" 204
as_a
call DELETE "/orgs/$ORG/projects/$PID";                           check "A deletes the project" 204
call DELETE "/orgs/$ORG/members/$SUB_B";                          check "A removes B" 204
call DELETE "/orgs/$ORG/members/$SUB_C";                          check "A removes C" 204
call DELETE "/orgs/$ORG";                                         check "A deletes the organization" 204

echo
echo "Result: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]