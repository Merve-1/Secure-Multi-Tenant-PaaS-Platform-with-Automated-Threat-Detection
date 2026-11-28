"""Approval workflow for tasks (the state machine itself is in core/workflow.py).

  POST .../tasks/{task_id}/submit           author or admin      draft | changes_requested -> submitted
  POST .../tasks/{task_id}/withdraw         submitter or admin   submitted -> draft
  POST .../tasks/{task_id}/approve          reviewer or admin    submitted -> approved
  POST .../tasks/{task_id}/request-changes  reviewer or admin    submitted -> changes_requested (comment required)
  POST .../tasks/{task_id}/reopen           admin                approved -> draft
  GET  .../tasks/{task_id}/history          any member           decisions and comments
  GET  /orgs/{org_id}/approvals             any member           the queue of tasks waiting for review

Enforcement:
  * The role check is in the handler (require) and the actor rule in may_perform:
    nobody reviews their own submission.
  * An invalid transition is a 409. The write itself is conditional on the task
    still being in the expected state, so two reviewers cannot both decide, and
    a submission cannot be decided after it was withdrawn.
  * One transaction updates the task, maintains the pending queue, appends the
    decision (with its comment) to the task's history and writes the audit event.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.auth import Member, Membership, require
from core.db import (PENDING_PREFIX, approval_prefix, get_task_or_404, now, pending_key,
                     query_page, run_tx, task_key, tpk, tx_audit, tx_delete, tx_put,
                     tx_update, used_only)
from core.log import log_denied, log_event
from core.permissions import allows
from core.schemas import Cursor, Limit, ProjectId, StrictModel, TaskId
from core.workflow import (REVIEW_ACTIONS, InvalidTransition, available_actions,
                           current_state, may_perform, next_state)

router = APIRouter(tags=["approvals"])

TASK_PATH = "/orgs/{org_id}/projects/{project_id}/tasks/{task_id}"
RACE = "Task changed. Reload and retry."

# Attribute names used in update expressions (every name is aliased).
FIELD_NAMES = {
    "#as": "approval_state", "#ua": "updated_at",
    "#sb": "submitted_by", "#sbe": "submitted_by_email", "#sa": "submitted_at",
    "#db": "decided_by", "#dbe": "decided_by_email", "#da": "decided_at",
    "#dc": "decision_comment",
}


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class CommentBody(StrictModel):
    comment: str | None = Field(default=None, max_length=1000)


class ChangesBody(StrictModel):
    comment: str = Field(min_length=1, max_length=1000)


class ApprovalResult(BaseModel):
    task_id: str
    project_id: str
    from_state: str
    to_state: str
    allowed_actions: list[str]


class EventOut(BaseModel):
    id: str
    action: str
    from_state: str
    to_state: str
    actor_email: str
    comment: str | None = None
    created_at: str


class EventList(BaseModel):
    items: list[EventOut]
    next_cursor: str | None = None


class PendingOut(BaseModel):
    task_id: str
    project_id: str
    title: str
    submitted_by_email: str
    submitted_at: str
    note: str | None = None


class PendingList(BaseModel):
    items: list[PendingOut]
    next_cursor: str | None = None


# --------------------------------------------------------------------------
# The transition
# --------------------------------------------------------------------------
def run_transition(m: Membership, pid: str, tid: str, action: str,
                   comment: str | None) -> ApprovalResult:
    task = get_task_or_404(m.org_id, pid, tid)

    if not may_perform(m, task, action):
        log_denied(f"authz.approval.{action}", m.org_id, m.caller.user_id,
                   role=m.role, resource_id=tid)
        # The handler already required the "review" permission, so for review
        # actions the only way to get here is reviewing your own submission.
        own_submission = action in REVIEW_ACTIONS and allows(m.role, "review")
        raise HTTPException(
            status_code=403,
            detail="You cannot review your own submission" if own_submission else "Forbidden")

    state = current_state(task)
    try:
        new_state = next_state(action, state)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    ts, event_id = now(), uuid.uuid4().hex
    user_id, email = m.caller.user_id, m.caller.email
    names: dict[str, str] = dict(FIELD_NAMES)
    values: dict[str, Any] = {":new": new_state, ":uid": user_id, ":email": email, ":ts": ts}
    extra_ops: list[dict] = []
    failures = {0: (409, RACE)}
    pending = (pending_key(m.org_id, task["submitted_at"], tid)
               if task.get("submitted_at") else None)

    if action == "submit":
        expression = ("SET #as = :new, #sb = :uid, #sbe = :email, #sa = :ts, #ua = :ts "
                      "REMOVE #db, #dbe, #da, #dc")
        condition = "attribute_exists(PK) AND (attribute_not_exists(#as) OR #as IN (:d, :cr))"
        values.update({":d": "draft", ":cr": "changes_requested"})
        if not allows(m.role, "task.write"):  # members: ownership repeated in the write
            values[":me"], names["#own"] = user_id, "owner"
            condition += " AND (created_by = :me OR #own = :email)"
        queue_item = {**pending_key(m.org_id, ts, tid), "entity_type": "pending_approval",
                      "task_id": tid, "project_id": pid, "title": task["title"],
                      "submitted_by": user_id, "submitted_by_email": email, "submitted_at": ts}
        if comment:
            queue_item["note"] = comment
        extra_ops.append(tx_put(queue_item))
        failures[1] = (409, "Task is already waiting for review")

    elif action in REVIEW_ACTIONS:
        expression = "SET #as = :new, #db = :uid, #dbe = :email, #da = :ts, #ua = :ts"
        if comment:
            expression += ", #dc = :comment"
            values[":comment"] = comment
        else:
            expression += " REMOVE #dc"
        condition = "attribute_exists(PK) AND #as = :submitted AND #sa = :sa AND #sb <> :uid"
        values.update({":submitted": "submitted", ":sa": task.get("submitted_at", "")})
        if pending:
            extra_ops.append(tx_delete(pending))

    elif action == "withdraw":
        expression = "SET #as = :new, #ua = :ts REMOVE #sb, #sbe, #sa"
        condition = "attribute_exists(PK) AND #as = :submitted AND #sa = :sa"
        values.update({":submitted": "submitted", ":sa": task.get("submitted_at", "")})
        if not allows(m.role, "task.write"):  # only the submitter, unless admin
            condition += " AND #sb = :uid"
        if pending:
            extra_ops.append(tx_delete(pending))

    else:  # reopen
        expression = ("SET #as = :new, #ua = :ts "
                      "REMOVE #sb, #sbe, #sa, #db, #dbe, #da, #dc")
        condition = "attribute_exists(PK) AND #as = :approved"
        values[":approved"] = "approved"

    use_names, use_values = used_only(expression + " " + condition, names, values)
    event = {**{"PK": tpk(m.org_id), "SK": f"{approval_prefix(tid)}{ts}#{event_id}"},
             "entity_type": "approval_event", "id": event_id, "task_id": tid,
             "project_id": pid, "action": action, "from_state": state,
             "to_state": new_state, "actor_id": user_id, "actor_email": email,
             "created_at": ts}
    if comment:
        event["comment"] = comment

    run_tx([
        tx_update(task_key(m.org_id, pid, tid), expression, condition, use_values, use_names),
        *extra_ops,
        tx_put(event),
        tx_audit(m.org_id, user_id, f"approval.{action}", tid, project_id=pid,
                 from_state=state, to_state=new_state),
    ], failures)
    log_event(f"approval.{action}", m.org_id, user_id, resource_id=tid, project_id=pid,
              from_state=state, to_state=new_state)

    fresh = get_task_or_404(m.org_id, pid, tid)
    return ApprovalResult(task_id=tid, project_id=pid, from_state=state, to_state=new_state,
                          allowed_actions=available_actions(m, fresh))


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post(TASK_PATH + "/submit", response_model=ApprovalResult)
def submit(project_id: ProjectId, task_id: TaskId, m: Member, body: CommentBody | None = None):
    require(m, "task.create")  # authors only; the ownership rule follows in may_perform
    return run_transition(m, str(project_id), str(task_id), "submit",
                          body.comment if body else None)


@router.post(TASK_PATH + "/withdraw", response_model=ApprovalResult)
def withdraw(project_id: ProjectId, task_id: TaskId, m: Member, body: CommentBody | None = None):
    require(m, "task.create")
    return run_transition(m, str(project_id), str(task_id), "withdraw",
                          body.comment if body else None)


@router.post(TASK_PATH + "/approve", response_model=ApprovalResult)
def approve(project_id: ProjectId, task_id: TaskId, m: Member, body: CommentBody | None = None):
    require(m, "review")
    return run_transition(m, str(project_id), str(task_id), "approve",
                          body.comment if body else None)


@router.post(TASK_PATH + "/request-changes", response_model=ApprovalResult)
def request_changes(project_id: ProjectId, task_id: TaskId, body: ChangesBody, m: Member):
    require(m, "review")
    return run_transition(m, str(project_id), str(task_id), "request_changes", body.comment)


@router.post(TASK_PATH + "/reopen", response_model=ApprovalResult)
def reopen(project_id: ProjectId, task_id: TaskId, m: Member, body: CommentBody | None = None):
    require(m, "approval.reopen")
    return run_transition(m, str(project_id), str(task_id), "reopen",
                          body.comment if body else None)


@router.get(TASK_PATH + "/history", response_model=EventList)
def history(project_id: ProjectId, task_id: TaskId, m: Member,
            limit: Limit = 50, cursor: Cursor = None):
    require(m, "task.read")
    tid = str(task_id)
    get_task_or_404(m.org_id, str(project_id), tid)
    page = query_page(m.org_id, approval_prefix(tid), limit, cursor)
    return {"items": [EventOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@router.get("/orgs/{org_id}/approvals", response_model=PendingList)
def pending_approvals(m: Member, limit: Limit = 50, cursor: Cursor = None):
    """Tasks waiting for review, oldest first. One Query, no Scan, no index."""
    require(m, "task.read")
    page = query_page(m.org_id, PENDING_PREFIX, limit, cursor)
    return {"items": [PendingOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}