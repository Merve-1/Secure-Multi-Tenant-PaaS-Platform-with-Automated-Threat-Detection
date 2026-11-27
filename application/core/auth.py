"""Who is calling, and what role do they have in this organization.

  * Identity (sub, email) comes only from claims API Gateway already verified.
  * The organization ID in the URL is just a lookup key. Access needs an ACTIVE
    membership item for the caller, otherwise the answer is 404, so the
    existence of other organizations is never revealed.
  * The role comes from that membership item in DynamoDB, never from the request
    or the token. Removing a member therefore takes effect on the next request.
  * Failed lookups are logged as access_denied so probing is visible.
"""
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPBearer

from core.db import get_item, member_key
from core.log import log_denied
from core.permissions import allows

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
    no email claim). Fails closed when there are no claims, for example if a
    route is ever exposed without the API Gateway authorizer."""
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
    """The single place that turns (caller, org ID from the URL) into a role."""
    item = get_item(member_key(org_id, caller.user_id))
    if not item or item.get("status", "active") != "active":
        log_denied("authz.membership", org_id, caller.user_id, reason="not_a_member")
        raise HTTPException(status_code=404, detail="Organization not found")
    return Membership(org_id=org_id, caller=caller, role=item["role"])


Member = Annotated[Membership, Depends(require_member)]


def require(m: Membership, permission: str) -> None:
    """Raise 403 (and log the attempt) unless the caller's role allows it."""
    if not allows(m.role, permission):
        log_denied(f"authz.{permission}", m.org_id, m.caller.user_id, role=m.role)
        raise HTTPException(status_code=403, detail="Forbidden")