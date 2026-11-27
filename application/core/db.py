"""DynamoDB access: key layout, queries, transactions and audit items.

Single-table layout (the tenant is always the partition key):
  Organization   PK=TENANT#<org>  SK=META
  Membership     PK=TENANT#<org>  SK=MEMBER#<user>         (role is stored here)
  Reverse link   PK=USER#<user>   SK=ORG#<org>
  Project        PK=TENANT#<org>  SK=PROJECT#<project>
  Task           PK=TENANT#<org>  SK=TASK#<project>#<task>
  Audit event    PK=TENANT#<org>  SK=AUDIT#<time>#<id>
  Pending review PK=TENANT#<org>  SK=PENDING#<submitted_at>#<task>
  Approval event PK=TENANT#<org>  SK=APPROVAL#<task>#<time>#<id>

This module knows nothing about HTTP routes or users.
"""
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from fastapi import HTTPException

META = "META"
PROJECT_PREFIX = "PROJECT#"
MEMBER_PREFIX = "MEMBER#"
PENDING_PREFIX = "PENDING#"


# --------------------------------------------------------------------------
# Connection and keys
# --------------------------------------------------------------------------
@lru_cache
def table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def table_name() -> str:
    return os.environ["TABLE_NAME"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def tpk(org_id: str) -> str:
    """Partition key for everything an organization (tenant) owns."""
    return f"TENANT#{org_id}"


def meta_key(org_id: str) -> dict:
    return {"PK": tpk(org_id), "SK": META}


def member_key(org_id: str, user_id: str) -> dict:
    return {"PK": tpk(org_id), "SK": f"{MEMBER_PREFIX}{user_id}"}


def link_key(user_id: str, org_id: str) -> dict:
    return {"PK": f"USER#{user_id}", "SK": f"ORG#{org_id}"}


def project_key(org_id: str, project_id: str) -> dict:
    return {"PK": tpk(org_id), "SK": f"{PROJECT_PREFIX}{project_id}"}


def task_prefix(project_id: str) -> str:
    return f"TASK#{project_id}#"


def task_key(org_id: str, project_id: str, task_id: str) -> dict:
    return {"PK": tpk(org_id), "SK": f"{task_prefix(project_id)}{task_id}"}


def pending_key(org_id: str, submitted_at: str, task_id: str) -> dict:
    """Queue of tasks waiting for review, oldest first. Lets the approvals list
    be a single Query with no Scan and no index."""
    return {"PK": tpk(org_id), "SK": f"{PENDING_PREFIX}{submitted_at}#{task_id}"}


def approval_prefix(task_id: str) -> str:
    """History of approval decisions for one task (task IDs are unique UUIDs)."""
    return f"APPROVAL#{task_id}#"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def get_item(key: dict) -> dict | None:
    return table().get_item(Key=key, ConsistentRead=True).get("Item")


def get_project_or_404(org_id: str, project_id: str) -> dict:
    item = get_item(project_key(org_id, project_id))
    if not item:
        raise HTTPException(status_code=404, detail="Project not found")
    return item


def get_task_or_404(org_id: str, project_id: str, task_id: str) -> dict:
    item = get_item(task_key(org_id, project_id, task_id))
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")
    return item


def has_items(org_id: str, prefix: str) -> bool:
    resp = table().query(
        KeyConditionExpression=Key("PK").eq(tpk(org_id)) & Key("SK").begins_with(prefix),
        Limit=1, ConsistentRead=True)
    return bool(resp["Items"])


def find_member_by_email(org_id: str, email: str) -> dict | None:
    """Find an active-or-not member of THIS organization by email. The query is
    confined to the organization's partition (at most MAX_MEMBERS_PER_ORG items),
    so it can never see another tenant."""
    resp = table().query(
        KeyConditionExpression=Key("PK").eq(tpk(org_id)) & Key("SK").begins_with(MEMBER_PREFIX),
        FilterExpression=Attr("email").eq(email), ConsistentRead=True)
    return resp["Items"][0] if resp["Items"] else None


def query_page(org_id: str, prefix: str, limit: int, cursor: str | None) -> dict:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(tpk(org_id)) & Key("SK").begins_with(prefix),
        "Limit": limit,
        "ConsistentRead": True,
    }
    if cursor:
        if not cursor.startswith(prefix):
            raise HTTPException(status_code=422, detail="Invalid cursor")
        # Only the sort key comes from the client. The partition key is rebuilt
        # here from the verified organization.
        kwargs["ExclusiveStartKey"] = {"PK": tpk(org_id), "SK": cursor}
    resp = table().query(**kwargs)
    last = resp.get("LastEvaluatedKey")
    return {"items": resp["Items"], "next_cursor": last["SK"] if last else None}


def build_update(fields: dict[str, Any]):
    """Build an UpdateExpression. Every attribute name is aliased because words
    such as 'name' and 'status' are reserved in DynamoDB. A value of None means
    REMOVE the attribute."""
    names, values, sets, removes = {}, {}, [], []
    for i, (key, value) in enumerate(fields.items()):
        names[f"#f{i}"] = key
        if value is None:
            removes.append(f"#f{i}")
        else:
            values[f":v{i}"] = value
            sets.append(f"#f{i} = :v{i}")
    parts = []
    if sets:
        parts.append("SET " + ", ".join(sets))
    if removes:
        parts.append("REMOVE " + ", ".join(removes))
    return " ".join(parts), names, values


_TOKEN = re.compile(r"[#:][A-Za-z0-9_]+")


def used_only(text: str, names: dict, values: dict):
    """DynamoDB rejects expression names/values that the expression does not
    use. Keep only the ones that appear in `text`."""
    tokens = set(_TOKEN.findall(text))
    return ({k: v for k, v in names.items() if k in tokens},
            {k: v for k, v in values.items() if k in tokens})


# --------------------------------------------------------------------------
# Transactions (every write also writes its audit event in the same transaction)
# --------------------------------------------------------------------------
def tx_put(item: dict) -> dict:
    return {"Put": {"TableName": table_name(), "Item": item,
                    "ConditionExpression": "attribute_not_exists(PK)"}}


def tx_delete(key: dict, condition=None, values=None, names=None) -> dict:
    op: dict[str, Any] = {"TableName": table_name(), "Key": key}
    if condition:
        op["ConditionExpression"] = condition
    if values:
        op["ExpressionAttributeValues"] = values
    if names:
        op["ExpressionAttributeNames"] = names
    return {"Delete": op}


def tx_update(key: dict, expression: str, condition: str, values: dict, names=None) -> dict:
    op: dict[str, Any] = {"TableName": table_name(), "Key": key,
                          "UpdateExpression": expression,
                          "ConditionExpression": condition,
                          "ExpressionAttributeValues": values}
    if names:
        op["ExpressionAttributeNames"] = names
    return {"Update": op}


def tx_audit(org_id: str, actor_id: str, action: str, target_id=None, **extra: Any) -> dict:
    """An audit event: who did what to which object. IDs only, no emails,
    tokens or request bodies."""
    ts, event_id = now(), uuid.uuid4().hex
    item = {"PK": tpk(org_id), "SK": f"AUDIT#{ts}#{event_id}", "entity_type": "audit",
            "id": event_id, "actor_id": actor_id, "action": action,
            "outcome": "success", "created_at": ts, **extra}
    if target_id:
        item["target_id"] = target_id
    return tx_put(item)


def run_tx(items: list[dict], failures: dict[int, tuple[int, str]]) -> None:
    """Run a transaction. `failures` maps the index of an operation to the
    (status, message) to return when that operation's condition fails."""
    try:
        table().meta.client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            for index, reason in enumerate(exc.response.get("CancellationReasons", [])):
                if reason.get("Code") == "ConditionalCheckFailed":
                    status, detail = failures.get(index, (409, "Conflict. Please retry."))
                    raise HTTPException(status_code=status, detail=detail)
            raise HTTPException(status_code=409, detail="Conflict. Please retry.")
        raise