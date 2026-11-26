#!/usr/bin/env bash
# test-orgs.sh - checks the organization and member endpoints on the DEPLOYED API.
#
# Needs two Cognito users that already exist in the pool (any tenant attribute):
#   TEST_EMAIL=a@example.com TEST_EMAIL_B=b@example.com ./scripts/test-orgs.sh
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
SUB_A="$(sub_of "$TOKEN_A")"; SUB_B="$(sub_of "$TOKEN_B")"
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

pass() { PASSED=$((PASSED + 1)); printf '  PASS  %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '  FAIL  %s\n        %s\n' "$1" "$(head -c 300 "$BODY_FILE")"; }

check() {  # check DESCRIPTION EXPECTED_STATUS
  if [ "$STATUS" = "$2" ]; then pass "$1 ($STATUS)"; else fail "$1 (expected $2, got $STATUS)"; fi
}
field() { python3 -c "import json;print(json.load(open('$BODY_FILE')).get('$1',''))"; }
check_field() {  # check_field DESCRIPTION FIELD EXPECTED
  if [ "$(field "$2")" = "$3" ]; then pass "$1"; else fail "$1 (expected $2=$3)"; fi
}
check_absent() {
  if grep -q "$2" "$BODY_FILE"; then fail "$1"; else pass "$1"; fi
}

echo "API: $API_URL"
echo
echo "Create and read an organization (user A)"
as_a
call POST /orgs '{"name":"Acme Corp"}';            check "create organization" 201
ORG="$(field id)"
if [ -z "$ORG" ]; then echo "No organization ID returned; stopping."; exit 1; fi
check_field "creator is admin" my_role admin
call GET /orgs;                                    check "list my organizations" 200
call GET "/orgs/$ORG";                             check "get organization" 200
call GET "/orgs/$ORG/members";                     check "list members" 200

echo
echo "Validation"
call POST /orgs '{}';                              check "missing name rejected" 422
call POST /orgs '{"name":"x","id":"evil"}';        check "client cannot set id" 422
call GET "/orgs/$(printf 'a%.0s' $(seq 1 65))";    check "oversized org id rejected" 422
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"owner\"}"
check "unknown role rejected" 422
call POST "/orgs/$ORG/members" '{"email":"not-an-email","role":"member"}'
check "bad email rejected" 422

echo
echo "Membership (user A is admin)"
call POST "/orgs/$ORG/members" "{\"email\":\"nobody-$RANDOM@example.com\",\"role\":\"member\"}"
check "unknown user is 404" 404
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"member\"}"
check "add user B as member" 201
call POST "/orgs/$ORG/members" "{\"email\":\"$TEST_EMAIL_B\",\"role\":\"member\"}"
check "adding twice is 409" 409

echo
echo "Roles (user B is a member)"
as_b
call GET "/orgs/$ORG";                             check "member can read org" 200
check_field "B sees role member" my_role member
call GET "/orgs/$ORG/members";                     check "member can list members" 200
call POST "/orgs/$ORG/members" '{"email":"x@example.com","role":"member"}'
check "member cannot add members" 403
call PATCH "/orgs/$ORG" '{"name":"Hijacked"}';     check "member cannot rename org" 403
call DELETE "/orgs/$ORG";                          check "member cannot delete org" 403
call PATCH "/orgs/$ORG/members/$SUB_A" '{"role":"member"}'
check "member cannot change roles" 403
as_a
call PATCH "/orgs/$ORG/members/$SUB_B" '{"role":"reviewer"}'
check "admin changes B to reviewer" 200
as_b
call GET "/orgs/$ORG";                             check "reviewer can read org" 200
check_field "B sees role reviewer" my_role reviewer
call POST "/orgs/$ORG/members" '{"email":"x@example.com","role":"member"}'
check "reviewer cannot add members" 403

echo
echo "Last-admin protection"
as_a
call PATCH "/orgs/$ORG/members/$SUB_A" '{"role":"member"}'
check "sole admin cannot demote self" 409
call DELETE "/orgs/$ORG/members/$SUB_A";           check "sole admin cannot leave" 409
call PATCH "/orgs/$ORG/members/$SUB_B" '{"role":"admin"}'
check "promote B to admin" 200
call PATCH "/orgs/$ORG/members/$SUB_A" '{"role":"member"}'
check "A can demote self once B is admin" 200
as_b
call PATCH "/orgs/$ORG/members/$SUB_A" '{"role":"admin"}'
check "B restores A as admin" 200

echo
echo "Isolation between organizations"
call POST /orgs '{"name":"Beta Inc"}';             check "B creates a second organization" 201
ORG2="$(field id)"
as_a
call GET "/orgs/$ORG2";                            check "A cannot read B's organization" 404
call GET "/orgs/$ORG2/members";                    check "A cannot list B's members" 404
call PATCH "/orgs/$ORG2" '{"name":"Hijacked"}';    check "A cannot rename B's organization" 404
call DELETE "/orgs/$ORG2";                         check "A cannot delete B's organization" 404
call GET /orgs;                                    check "A lists organizations" 200
check_absent "A's list does not include B's organization" "$ORG2"

echo
echo "Cleanup"
call DELETE "/orgs/$ORG";                          check "cannot delete org with other members" 409
call DELETE "/orgs/$ORG/members/$SUB_B";           check "A removes B" 204
call DELETE "/orgs/$ORG";                          check "A deletes organization" 204
call GET "/orgs/$ORG";                             check "deleted organization is gone" 404
as_b
call DELETE "/orgs/$ORG2";                         check "B deletes second organization" 204

echo
echo "Result: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]