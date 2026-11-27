"""Authorization tests. They need no AWS access.

Part 1 checks the helper itself: identity comes from the verified token, and the
role comes from the stored membership, never from anything the client sends.

Part 2 audits the whole app, so a new endpoint that skips the helper, or checks
membership but forgets require(), makes a test fail instead of shipping.

Run from the repository root:
    pip install -r application/requirements.txt pytest boto3
    pytest
"""
import inspect

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

import core.auth as auth
from app import app
from core.auth import (Caller, Membership, can_modify_task, get_caller, require,
                       require_member, require_task_access)
from core.schemas import StrictModel

CALLER = Caller(user_id="user-1", email="a@example.com")


# ---------------------------------------------------------------- the helper
def request_with_claims(claims: dict, headers=None) -> Request:
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    return Request({"type": "http", "aws.event": event, "headers": headers or []})


def test_identity_comes_only_from_verified_claims():
    request = request_with_claims({"sub": "user-1", "email": "A@Example.com"})
    caller = get_caller(request, None)
    assert caller == Caller(user_id="user-1", email="a@example.com")


def test_missing_subject_is_unauthorized():
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with_claims({}), None)
    assert exc.value.status_code == 401


def test_token_without_email_is_rejected():
    with pytest.raises(HTTPException) as exc:
        get_caller(request_with_claims({"sub": "user-1"}), None)
    assert exc.value.status_code == 403


def test_tenant_claims_and_headers_are_ignored():
    # The old design trusted custom:tenant_id. Nothing like that is read any more.
    request = request_with_claims(
        {"sub": "user-1", "email": "a@example.com",
         "custom:tenant_id": "org-evil", "cognito:groups": "[admin]"},
        headers=[(b"x-tenant-id", b"org-evil"), (b"x-org-id", b"org-evil")])
    assert get_caller(request, None) == CALLER


def test_non_member_gets_404(monkeypatch):
    monkeypatch.setattr(auth, "get_item", lambda key: None)
    with pytest.raises(HTTPException) as exc:
        require_member("org-1", CALLER)
    assert exc.value.status_code == 404


def test_membership_is_looked_up_with_the_verified_caller(monkeypatch):
    seen = {}

    def fake_get_item(key):
        seen["key"] = key
        return {"role": "reviewer", "status": "active"}

    monkeypatch.setattr(auth, "get_item", fake_get_item)
    membership = require_member("org-1", CALLER)
    assert membership.role == "reviewer"
    assert membership.org_id == "org-1"
    assert seen["key"] == {"PK": "TENANT#org-1", "SK": "MEMBER#user-1"}


def test_inactive_membership_gets_404(monkeypatch):
    monkeypatch.setattr(auth, "get_item",
                        lambda key: {"role": "admin", "status": "suspended"})
    with pytest.raises(HTTPException) as exc:
        require_member("org-1", CALLER)
    assert exc.value.status_code == 404


def test_unknown_stored_role_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "get_item",
                        lambda key: {"role": "superuser", "status": "active"})
    with pytest.raises(HTTPException) as exc:
        require_member("org-1", CALLER)
    assert exc.value.status_code == 403


def test_require_checks_the_role_permission():
    require(Membership("org-1", CALLER, "admin"), "project.delete")  # allowed
    require(Membership("org-1", CALLER, "member"), "project.write")  # allowed
    for role, permission in [("reviewer", "project.write"),
                             ("member", "project.delete"),
                             ("member", "member.manage"),
                             ("reviewer", "task.write")]:
        with pytest.raises(HTTPException) as exc:
            require(Membership("org-1", CALLER, role), permission)
        assert exc.value.status_code == 403


# ------------------------------------------------------------- task ownership
def membership(role: str) -> Membership:
    return Membership("org-1", CALLER, role)


TASK_MINE = {"id": "t1", "created_by": "user-1", "owner": "someone@example.com"}
TASK_ASSIGNED = {"id": "t2", "created_by": "user-9", "owner": "a@example.com"}
TASK_OTHERS = {"id": "t3", "created_by": "user-9", "owner": "other@example.com"}


def test_admin_may_modify_any_task():
    for task in (TASK_MINE, TASK_ASSIGNED, TASK_OTHERS):
        for action in ("write", "delete"):
            assert can_modify_task(membership("admin"), task, action)


def test_member_may_modify_only_tasks_they_created_or_are_assigned():
    m = membership("member")
    assert can_modify_task(m, TASK_MINE, "write")
    assert can_modify_task(m, TASK_MINE, "delete")
    assert can_modify_task(m, TASK_ASSIGNED, "write")
    assert not can_modify_task(m, TASK_OTHERS, "write")
    assert not can_modify_task(m, TASK_OTHERS, "delete")


def test_reviewer_may_never_modify_a_task_even_their_own():
    m = membership("reviewer")
    assert not can_modify_task(m, TASK_MINE, "write")
    assert not can_modify_task(m, TASK_MINE, "delete")


def test_require_task_access_is_403_for_someone_elses_task():
    with pytest.raises(HTTPException) as exc:
        require_task_access(membership("member"), TASK_OTHERS, "delete")
    assert exc.value.status_code == 403
    require_task_access(membership("member"), TASK_MINE, "delete")  # no error


# ------------------------------------------------------- the whole-app audit
PUBLIC = {("GET", "/"), ("GET", "/health")}
IDENTITY_ONLY = {("GET", "/me"), ("GET", "/orgs"), ("POST", "/orgs")}


def api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route.path, route


def dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from dependency_calls(dep)


def test_there_are_routes_to_audit():
    assert len(list(api_routes())) >= 20


def test_every_route_authenticates_the_caller_unless_explicitly_public():
    for method, path, route in api_routes():
        if (method, path) in PUBLIC:
            continue
        calls = set(dependency_calls(route.dependant))
        assert get_caller in calls, f"{method} {path} does not authenticate the caller"


def test_every_organization_route_uses_the_membership_gate_and_a_permission():
    for method, path, route in api_routes():
        if (method, path) in PUBLIC | IDENTITY_ONLY:
            continue
        assert path.startswith("/orgs/{org_id}"), \
            f"{method} {path} is not organization-scoped and not on the allowlist"
        calls = set(dependency_calls(route.dependant))
        assert require_member in calls, f"{method} {path} skips require_member"
        source = inspect.getsource(route.endpoint)
        assert "require(" in source or "require_task_access(" in source, \
            f"{method} {path} checks membership but never checks a permission"


def test_no_request_body_accepts_a_tenant_or_organization_id():
    forbidden = {"tenant_id", "org_id", "organization_id", "tenant", "user_id"}
    models = list(StrictModel.__subclasses__())
    assert models, "no request models found"
    for model in models:
        overlap = forbidden & set(model.model_fields)
        assert not overlap, f"{model.__name__} accepts {sorted(overlap)} from the client"


def test_request_models_reject_unknown_fields():
    for model in StrictModel.__subclasses__():
        assert model.model_config.get("extra") == "forbid", model.__name__