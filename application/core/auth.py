"""Who is calling, and what role do they have in this organization.

  * Identity (sub, email) comes only from claims API Gateway already verified.
  * The organization ID in the URL is just a lookup key. Access needs a
    membership item for the caller, otherwise the answer is 404, so the
    existence of other organizations is never revealed.
  * The role comes from that membership item in DynamoDB, never from the request.

Rules for every endpoint (tests/unit/test_authorization.py enforces them):
  1. Organization-scoped routes live under /orgs/{org_id}/... and take the
     `Member` dependency, which is the only way to obtain an organization ID.
  2. The handler then calls require(m, "<permission>") before doing anything.
  3. Handlers use m.org_id for every key. No request body, query string or
     header may carry a tenant or organization ID.
  4. Routes without an organization (/orgs, /me) use only the verified caller.
"""
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPBearer

from core.db import get_item, member_key
from core.log import log_denied
from core.permissions import PERMISSIONS, allows

# Shows the "Authorize" button in /docs. API Gateway already enforces the token,
# so this scheme does not reject anything itself.
bearer_scheme = HTTPBearer(auto_error=False)

OrgId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Organization ID")]


@dataclass(frozen=True)
class Caller:
    user_id: str
    email: str


@dataclass(frozen=True)
class Membership:
    org_id: str
    caller: Caller
    role: str


def get_caller(request: Request,
               _credentials: Annotated[Any, Depends(bearer_scheme)]) -> Caller:
    """Identity from the verified JWT. Needs an ID token (the access token has
    no email claim)."""
    claims = (request.scope.get("aws.event", {})
              .get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}))
    user_id, email = claims.get("sub"), claims.get("email")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not email:
        raise HTTPException(status_code=403, detail="Forbidden")
    return Caller(user_id=user_id, email=email.lower())


CallerDep = Annotated[Caller, Depends(get_caller)]


def require_member(org_id: OrgId, caller: CallerDep) -> Membership:
    """The one gate every organization-scoped endpoint goes through.

    org_id comes from the URL, so it is untrusted input. It is only used as a
    lookup key: the caller must have an active membership item for it, otherwise
    the answer is 404 and the attempt is logged (this is the signal for
    detecting repeated cross-tenant access attempts). The role is read from that
    item, never from the request.
    """
    item = get_item(member_key(org_id, caller.user_id))
    if not item or item.get("status", "active") != "active":
        log_denied("authz.not_member", None, caller.user_id, requested_org=org_id)
        raise HTTPException(status_code=404, detail="Organization not found")
    role = item.get("role")
    if role not in PERMISSIONS:
        log_denied("authz.invalid_role", org_id, caller.user_id, role=str(role))
        raise HTTPException(status_code=403, detail="Forbidden")
    return Membership(org_id=org_id, caller=caller, role=role)


Member = Annotated[Membership, Depends(require_member)]


def require(m: Membership, permission: str) -> None:
    """Raise 403 (and log the attempt) unless the caller's role allows it."""
    if not allows(m.role, permission):
        log_denied(f"authz.{permission}", m.org_id, m.caller.user_id, role=m.role)
        raise HTTPException(status_code=403, detail="Forbidden")


# --------------------------------------------------------------------------
# Task ownership
# --------------------------------------------------------------------------
def is_task_owner(m: Membership, task: dict) -> bool:
    """The caller created the task or is assigned to it."""
    return task.get("created_by") == m.caller.user_id or task.get("owner") == m.caller.email


def can_modify_task(m: Membership, task: dict, action: str) -> bool:
    """action is "write" or "delete".

    A role with the unrestricted permission (admin) may modify any task in the
    organization. A role with only the "_own" variant (member) may modify the
    tasks it created or is assigned to. Everyone else (reviewer) may not.
    """
    if allows(m.role, f"task.{action}"):
        return True
    return allows(m.role, f"task.{action}_own") and is_task_owner(m, task)


def require_task_access(m: Membership, task: dict, action: str) -> None:
    """Raise 403 (and log it) unless the caller may modify this task."""
    if not can_modify_task(m, task, action):
        log_denied(f"authz.task.{action}", m.org_id, m.caller.user_id,
                   role=m.role, resource_id=task.get("id"))
        raise HTTPException(status_code=403, detail="Forbidden")