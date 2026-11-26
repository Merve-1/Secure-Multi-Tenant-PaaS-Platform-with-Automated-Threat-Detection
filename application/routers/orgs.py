"""Organizations and memberships.

Changes that touch several items run in ONE DynamoDB transaction together with
their audit event. admin_count lives on the organization item and is checked
inside the transaction, so two admins cannot demote each other at the same time
and leave an organization without an admin.
"""
import os
import uuid
from functools import lru_cache
from typing import Annotated, Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Path, Response
from pydantic import BaseModel, Field

from core.auth import CallerDep, Member, require
from core.db import (MEMBER_PREFIX, PROJECT_PREFIX, get_item, has_items, link_key,
                     member_key, meta_key, now, query_page, run_tx, table, tpk,
                     tx_audit, tx_delete, tx_put, tx_update)
from core.log import log_event
from core.permissions import Role
from core.schemas import Cursor, Limit, StrictModel

router = APIRouter(tags=["organizations"])

MAX_ORGS_PER_USER = 10
MAX_MEMBERS_PER_ORG = 200

UserId = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,64}$", description="User ID (Cognito sub)")]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class OrgCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)


class OrgUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=100)


class MemberAdd(StrictModel):
    email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Role = Role.member


class RoleChange(StrictModel):
    role: Role


class OrgOut(BaseModel):
    id: str
    name: str
    member_count: int
    created_at: str
    updated_at: str
    my_role: Role | None = None


class MemberOut(BaseModel):
    user_id: str
    email: str
    role: Role
    status: str
    joined_at: str


class OrgList(BaseModel):
    items: list[OrgOut]


class MemberList(BaseModel):
    items: list[MemberOut]
    next_cursor: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
@lru_cache
def cognito():
    return boto3.client("cognito-idp")


def org_out(meta: dict, role: str | None) -> OrgOut:
    return OrgOut(id=meta["id"], name=meta["name"],
                  member_count=int(meta.get("member_count", 0)),
                  created_at=meta["created_at"], updated_at=meta["updated_at"],
                  my_role=role)


def fetch_meta(org_id: str) -> dict:
    meta = get_item(meta_key(org_id))
    if not meta:
        raise HTTPException(status_code=404, detail="Organization not found")
    return meta


def fetch_member(org_id: str, user_id: str) -> dict:
    item = get_item(member_key(org_id, user_id))
    if not item:
        raise HTTPException(status_code=404, detail="Member not found")
    return item


def count_memberships(user_id: str) -> int:
    resp = table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("ORG#"),
        Select="COUNT", ConsistentRead=True)
    return resp["Count"]


def find_user(email: str) -> dict | None:
    """Look a user up in Cognito by email. None if there is no enabled user."""
    try:
        resp = cognito().admin_get_user(UserPoolId=os.environ["USER_POOL_ID"], Username=email)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return None
        raise
    if not resp.get("Enabled", True):
        return None
    attrs = {a["Name"]: a["Value"] for a in resp["UserAttributes"]}
    return {"sub": attrs["sub"], "email": attrs.get("email", email)}


def tx_counts(org_id: str, members: int = 0, admins: int = 0,
              condition: str = "attribute_exists(PK)", values=None) -> dict:
    """Adjust member_count / admin_count on the organization item."""
    adds, vals = [], dict(values or {})
    if members:
        adds.append("member_count :m")
        vals[":m"] = members
    if admins:
        adds.append("admin_count :a")
        vals[":a"] = admins
    return tx_update(meta_key(org_id), "ADD " + ", ".join(adds), condition, vals)


# --------------------------------------------------------------------------
# Organizations
# --------------------------------------------------------------------------
@router.post("/orgs", response_model=OrgOut, status_code=201)
def create_org(body: OrgCreate, caller: CallerDep):
    if count_memberships(caller.user_id) >= MAX_ORGS_PER_USER:
        raise HTTPException(status_code=409, detail="Organization limit reached")
    org_id, ts = "org-" + uuid.uuid4().hex[:16], now()
    meta = {**meta_key(org_id), "entity_type": "org", "id": org_id, "name": body.name,
            "created_by": caller.user_id, "created_at": ts, "updated_at": ts,
            "member_count": 1, "admin_count": 1}
    member = {**member_key(org_id, caller.user_id), "entity_type": "member",
              "user_id": caller.user_id, "email": caller.email, "role": "admin",
              "status": "active", "joined_at": ts}
    link = {**link_key(caller.user_id, org_id), "entity_type": "membership",
            "org_id": org_id, "role": "admin", "joined_at": ts}
    run_tx([tx_put(meta), tx_put(member), tx_put(link),
            tx_audit(org_id, caller.user_id, "org.create", org_id)], {})
    log_event("org.create", org_id, caller.user_id)
    return org_out(meta, "admin")


@router.get("/orgs", response_model=OrgList)
def list_orgs(caller: CallerDep):
    links = table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{caller.user_id}") & Key("SK").begins_with("ORG#"),
        Limit=MAX_ORGS_PER_USER, ConsistentRead=True)["Items"]
    items = []
    for link in links:
        meta = get_item(meta_key(link["org_id"]))
        if meta:
            items.append(org_out(meta, link["role"]))
    return {"items": items}


@router.get("/orgs/{org_id}", response_model=OrgOut)
def get_org(m: Member):
    require(m, "org.read")
    return org_out(fetch_meta(m.org_id), m.role)


@router.patch("/orgs/{org_id}", response_model=OrgOut)
def update_org(body: OrgUpdate, m: Member):
    require(m, "org.update")
    run_tx([
        tx_update(meta_key(m.org_id), "SET #n = :n, updated_at = :ts",
                  "attribute_exists(PK)", {":n": body.name, ":ts": now()}, {"#n": "name"}),
        tx_audit(m.org_id, m.caller.user_id, "org.update", m.org_id),
    ], {0: (404, "Organization not found")})
    log_event("org.update", m.org_id, m.caller.user_id)
    return org_out(fetch_meta(m.org_id), m.role)


@router.delete("/orgs/{org_id}", status_code=204, response_class=Response)
def delete_org(m: Member):
    require(m, "org.delete")
    if has_items(m.org_id, PROJECT_PREFIX):
        raise HTTPException(status_code=409, detail="Delete the organization's projects first")
    run_tx([
        tx_delete(meta_key(m.org_id), "member_count = :one", {":one": 1}),
        tx_delete(member_key(m.org_id, m.caller.user_id)),
        tx_delete(link_key(m.caller.user_id, m.org_id)),
        tx_audit(m.org_id, m.caller.user_id, "org.delete", m.org_id),
    ], {0: (409, "Remove the other members first")})
    log_event("org.delete", m.org_id, m.caller.user_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------
@router.get("/orgs/{org_id}/members", response_model=MemberList)
def list_members(m: Member, limit: Limit = 50, cursor: Cursor = None):
    require(m, "member.read")
    page = query_page(m.org_id, MEMBER_PREFIX, limit, cursor)
    return {"items": [MemberOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@router.post("/orgs/{org_id}/members", response_model=MemberOut, status_code=201)
def add_member(body: MemberAdd, m: Member):
    require(m, "member.manage")
    user = find_user(body.email.lower())
    if not user:
        raise HTTPException(status_code=404, detail="No registered user with that email")
    if count_memberships(user["sub"]) >= MAX_ORGS_PER_USER:
        raise HTTPException(status_code=409, detail="That user has reached the organization limit")
    role, ts = body.role.value, now()
    member = {**member_key(m.org_id, user["sub"]), "entity_type": "member",
              "user_id": user["sub"], "email": user["email"].lower(), "role": role,
              "status": "active", "joined_at": ts}
    link = {**link_key(user["sub"], m.org_id), "entity_type": "membership",
            "org_id": m.org_id, "role": role, "joined_at": ts}
    run_tx([
        tx_counts(m.org_id, members=1, admins=1 if role == "admin" else 0,
                  condition="attribute_exists(PK) AND member_count < :max",
                  values={":max": MAX_MEMBERS_PER_ORG}),
        tx_put(member),
        tx_put(link),
        tx_audit(m.org_id, m.caller.user_id, "member.add", user["sub"], role=role),
    ], {0: (409, "Organization has reached its member limit"),
        1: (409, "User is already a member")})
    log_event("member.add", m.org_id, m.caller.user_id, target_id=user["sub"], role=role)
    return MemberOut(**member)


@router.patch("/orgs/{org_id}/members/{user_id}", response_model=MemberOut)
def change_role(user_id: UserId, body: RoleChange, m: Member):
    require(m, "member.manage")
    target = fetch_member(m.org_id, user_id)
    old, new = target["role"], body.role.value
    if old == new:
        return MemberOut(**target)
    names = {"#r": "role"}
    items: list[dict[str, Any]] = [
        tx_update(member_key(m.org_id, user_id), "SET #r = :new",
                  "attribute_exists(PK) AND #r = :old", {":new": new, ":old": old}, names),
        tx_update(link_key(user_id, m.org_id), "SET #r = :new",
                  "attribute_exists(PK)", {":new": new}, names),
    ]
    failures = {0: (409, "Role changed concurrently. Reload and retry.")}
    if old == "admin":
        failures[len(items)] = (409, "An organization needs at least one admin")
        items.append(tx_counts(m.org_id, admins=-1, condition="admin_count > :one",
                               values={":one": 1}))
    elif new == "admin":
        items.append(tx_counts(m.org_id, admins=1))
    items.append(tx_audit(m.org_id, m.caller.user_id, "member.role_change", user_id,
                          old_role=old, new_role=new))
    run_tx(items, failures)
    log_event("member.role_change", m.org_id, m.caller.user_id, target_id=user_id,
              old_role=old, new_role=new)
    return MemberOut(**{**target, "role": new})


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204, response_class=Response)
def remove_member(user_id: UserId, m: Member):
    own = user_id == m.caller.user_id
    if not own:
        require(m, "member.manage")  # anyone may leave an organization themselves
    target = fetch_member(m.org_id, user_id)
    role = target["role"]
    is_admin = role == "admin"
    action = "member.leave" if own else "member.remove"
    items = [
        tx_delete(member_key(m.org_id, user_id), "#r = :r", {":r": role}, {"#r": "role"}),
        tx_delete(link_key(user_id, m.org_id)),
        tx_counts(m.org_id, members=-1, admins=-1 if is_admin else 0,
                  condition="admin_count > :one" if is_admin else "attribute_exists(PK)",
                  values={":one": 1} if is_admin else None),
        tx_audit(m.org_id, m.caller.user_id, action, user_id, role=role),
    ]
    failures = {0: (409, "Member changed. Reload and retry.")}
    if is_admin:
        failures[2] = (409, "An organization needs at least one admin")
    run_tx(items, failures)
    log_event(action, m.org_id, m.caller.user_id, target_id=user_id, role=role)
    return Response(status_code=204)