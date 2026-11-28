#!/usr/bin/env bash
# test-approvals.sh - checks the approval workflow on the DEPLOYED API.
# Uses three existing Cognito users:
#
#   A = organization admin, B = member (the author), C = reviewer
#
#   TEST_EMAIL=a@x.com TEST_EMAIL_B=b@x.com TEST_EMAIL_C=c@x.com ./tests/e2e/test-approvals.sh
#
# Passwords are typed at a prompt and are never stored or printed.
# The script removes everything it creates.

set -uo pipefail
STACK=sam-fastapi
REGION=eu-north-1

: "${TEST_EMAIL:?Set TEST_EMAIL (user A, admin)}"
: "${TEST_EMAIL_B:?Set TEST_EMAIL_B (user B, member)}"
: "${TEST_EMAIL_C:?Set TEST_EMAIL_C (user C, reviewer)}"

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
check_has() { if grep -q "$2" "$BODY_FILE"; then pass "$1"; else fail "$1 (missing $2)"; fi; }
check_absent() { if grep -q "$2" "$BODY_FILE"; then fail "$1 (found $2)"; else pass "$1"; fi; }

echo "API: $API_URL"
echo
echo "Setup"
as_a
call POST /orgs '{"name":"Approval Test Org"}';                      check "A creates organization" 201
ORG="$(field id)"
if [ -z "$ORG" ]; then echo "No organization ID returned; stopping."; exit 1; fi
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"member\"}"
check "A adds B as member" 201
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_C\",\"role\":\"reviewer\"}"
check "A adds C as reviewer" 201
call POST "/orgs/$ORG/projects" '{"name":"Review project"}';         check "A creates a project" 201
PID="$(field id)"
T="/orgs/$ORG/projects/$PID/tasks"
APPROVALS="/orgs/$ORG/approvals"
as_b
call POST "$T" '{"title":"Audit logging implementation"}';           check "B creates a task" 201
TID="$(field id)"
check_field "new task is a draft" approval_state draft
check_field "B can submit it" allowed_actions "['submit']"
WF="$T/$TID"

echo
echo "Submitting"
as_c
call POST "$WF/submit";                                              check "reviewer cannot submit" 403
call POST "$WF/approve";                                             check "cannot approve a draft" 409
as_b
call POST "$WF/approve";                                             check "member cannot approve" 403
call POST "$WF/submit" '{"comment":"Ready for review"}';             check "B submits the task" 200
check_field "state is submitted" to_state submitted
call POST "$WF/submit";                                              check "cannot submit twice" 409
call GET "$WF";                                                      check "B reads the task" 200
check_field "task shows submitted" approval_state submitted
check_field "submitted task is locked for B" can_edit False

echo
echo "Locking while in review"
call PATCH "$WF" '{"title":"changed while locked"}';                  check "cannot edit a submitted task" 409
call DELETE "$WF";                                                   check "cannot delete a submitted task" 409

echo
echo "The review queue"
as_c
call GET "$APPROVALS";                                               check "reviewer sees the queue" 200
check_has "queue lists the task" "$TID"
check_has "queue shows the title" "Audit logging implementation"
call GET "$WF";                                                      check "reviewer reads the task" 200
check_field "reviewer can approve or request changes" allowed_actions "['approve', 'request_changes']"

echo
echo "Decisions"
call POST "$WF/request-changes" '{}';                                check "comment is required to request changes" 422
call POST "$WF/request-changes" '{"comment":"Please add tests"}';    check "C requests changes" 200
check_field "state is changes_requested" to_state changes_requested
call POST "$WF/approve";                                             check "cannot decide twice" 409
call GET "$APPROVALS";                                               check "queue after decision" 200
check_absent "task left the queue" "$TID"
as_b
call GET "$WF";                                                      check "B reads the task" 200
check_field "B sees the comment" decision_comment "Please add tests"
check_field "task is editable again" can_edit True
call PATCH "$WF" '{"description":"tests added"}';                    check "B edits after changes requested" 200
call POST "$WF/submit";                                              check "B resubmits" 200
as_c
call POST "$WF/approve" '{"comment":"Looks good"}';                  check "C approves" 200
check_field "state is approved" to_state approved
call GET "$APPROVALS";                                               check "queue after approval" 200
check_absent "approved task left the queue" "$TID"

echo
echo "Approved tasks are locked"
as_b
call PATCH "$WF" '{"title":"edit after approval"}';                  check "cannot edit an approved task" 409
call DELETE "$WF";                                                   check "cannot delete an approved task" 409
call POST "$WF/withdraw";                                            check "cannot withdraw an approved task" 409
call POST "$WF/reopen";                                              check "member cannot reopen" 403
as_c
call POST "$WF/reopen";                                              check "reviewer cannot reopen" 403

echo
echo "History"
as_b
call GET "$WF/history";                                              check "B reads the history" 200
check_has "history has the submission" "submit"
check_has "history has the change request" "request_changes"
check_has "history keeps the comment" "Please add tests"
check_has "history has the approval comment" "Looks good"

echo
echo "Separation of duties and reopening (admin)"
as_a
call POST "$WF/reopen";                                              check "admin reopens the approved task" 200
check_field "reopened task is a draft" to_state draft
call POST "$T" '{"title":"Admin task"}';                             check "A creates a task" 201
ATID="$(field id)"
call POST "$T/$ATID/submit";                                         check "A submits own task" 200
call POST "$T/$ATID/approve";                                        check "A cannot approve own submission" 403
call POST "$T/$ATID/request-changes" '{"comment":"x"}';              check "A cannot request changes on own submission" 403
as_c
call POST "$T/$ATID/approve";                                        check "reviewer approves A's task" 200
as_a
call POST "$T/$ATID/reopen";                                         check "A reopens it" 200

echo
echo "Withdrawing"
as_b
call POST "$WF/submit";                                              check "B submits again" 200
as_c
call POST "$WF/withdraw";                                            check "reviewer cannot withdraw" 403
as_b
call POST "$WF/withdraw";                                            check "B withdraws own submission" 200
check_field "back to draft" to_state draft
as_c
call GET "$APPROVALS";                                               check "queue after withdrawal" 200
check_absent "withdrawn task left the queue" "$TID"

echo
echo "Isolation between organizations"
as_b
call POST /orgs '{"name":"B Org"}';                                  check "B creates own organization" 201
ORG2="$(field id)"
call POST "/orgs/$ORG2/projects" '{"name":"B project"}';             check "B creates a project there" 201
PID2="$(field id)"
call POST "/orgs/$ORG2/projects/$PID2/tasks" '{"title":"B secret"}'; check "B creates a task there" 201
TID2="$(field id)"
T2="/orgs/$ORG2/projects/$PID2/tasks/$TID2"
call POST "$T2/submit";                                              check "B submits it" 200
as_c
call GET "/orgs/$ORG2/approvals";                                    check "C cannot see B's review queue" 404
call POST "$T2/approve";                                             check "C cannot approve in B's org" 404
call POST "$T/$TID2/approve";                                        check "B's task ID does not exist in our org" 404
call GET "$APPROVALS";                                               check "our queue" 200
check_absent "other org's task is not in our queue" "$TID2"
as_a
call POST "$T2/approve";                                             check "A cannot approve in B's org" 404

echo
echo "Cleanup"
as_b
call POST "$T2/withdraw";                                            check "B withdraws in own org" 200
call DELETE "$T2";                                                   check "B deletes own task" 204
call DELETE "/orgs/$ORG2/projects/$PID2";                            check "B deletes own project" 204
call DELETE "/orgs/$ORG2";                                           check "B deletes own organization" 204
call DELETE "$WF";                                                   check "B deletes the first task" 204
as_a
call DELETE "$T/$ATID";                                              check "A deletes own task" 204
call DELETE "/orgs/$ORG/projects/$PID";                              check "A deletes the project" 204
call DELETE "/orgs/$ORG/members/$SUB_B";                             check "A removes B" 204
call DELETE "/orgs/$ORG/members/$SUB_C";                             check "A removes C" 204
call DELETE "/orgs/$ORG";                                            check "A deletes the organization" 204

echo
echo "Result: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]