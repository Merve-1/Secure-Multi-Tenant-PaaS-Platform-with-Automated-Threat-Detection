"""Tasks, scoped to a project in an organization:
/orgs/{org_id}/projects/{project_id}/tasks

Isolation, ownership and locking rules:
  * Every key is built from the verified organization (m.org_id), so a task of
    another organization cannot be reached whatever IDs the client sends.
  * Admins may change or delete any task. Members only the tasks they created or
    are assigned to. Reviewers may only read.
  * A task that is submitted or approved is LOCKED: no edit, no delete. Reviewers
    always decide on exactly what was submitted. (Workflow endpoints live in
    routers/approvals.py.)
  * Ownership and the lock are checked in the handler (clean 403/409) AND
    repeated inside the write's ConditionExpression, so a race between the read
    and the write cannot bypass them.
  * A task's owner must be an email address of a member of the organization.
  * Every write carries its audit event in the same transaction.
"""
import uuid
from datetime import date
from enum import Enum

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from core.auth import Member, Membership, can_modify_task, require, require_task_access
from core.db import (build_update, find_member_by_email, get_project_or_404,
                     get_task_or_404, now, query_page, run_tx, task_key, task_prefix,
                     tx_audit, tx_delete, tx_put, tx_update)
from core.guards import own_guard, unlocked_guard
from core.log import log_event
from core.schemas import Cursor, Limit, ProjectId, StrictModel, TaskId
from core.workflow import ApprovalState, available_actions, current_state, is_locked

router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/tasks", tags=["tasks"])

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
CHANGED_HANDS = "Task changed, is locked, or is no longer yours. Reload and retry."
LOCKED = "Task is locked while it is submitted or approved"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class TaskCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    status: TaskStatus = TaskStatus.todo
    due_date: date | None = None


class TaskUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=320, pattern=EMAIL_PATTERN)
    status: TaskStatus | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def check_fields(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        for field in ("title", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class TaskOut(BaseModel):
    id: str
    project_id: str
    title: str
    description: str | None = None
    owner: str | None = None
    status: TaskStatus
    due_date: date | None = None
    created_by: str
    created_at: str
    updated_at: str
    # Approval workflow (changed only through routers/approvals.py)
    approval_state: ApprovalState = ApprovalState.draft
    submitted_by_email: str | None = None
    submitted_at: str | None = None
    decided_by_email: str | None = None
    decided_at: str | None = None
    decision_comment: str | None = None
    # What the CALLER may do with this task, computed by the server so the
    # frontend never has to re-implement the rules.
    can_edit: bool = False
    can_delete: bool = False
    allowed_actions: list[str] = []


class TaskList(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def task_out(m: Membership, item: dict) -> TaskOut:
    unlocked = not is_locked(current_state(item))
    return TaskOut(**item,
                   can_edit=unlocked and can_modify_task(m, item, "write"),
                   can_delete=unlocked and can_modify_task(m, item, "delete"),
                   allowed_actions=available_actions(m, item))


def resolve_owner(org_id: str, email: str) -> str:
    """An owner must be an active member of THIS organization."""
    owner = email.lower()
    member = find_member_by_email(org_id, owner)
    if not member or member.get("status", "active") != "active":
        raise HTTPException(status_code=422,
                            detail="Owner must be the email of a member of this organization")
    return owner


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post("", response_model=TaskOut, status_code=201)
def create_task(project_id: ProjectId, body: TaskCreate, m: Member):
    require(m, "task.create")
    pid = str(project_id)
    get_project_or_404(m.org_id, pid)  # 404 if missing or in another organization
    owner = (body.owner or m.caller.email).lower()
    if owner != m.caller.email:
        owner = resolve_owner(m.org_id, owner)
    task_id, ts = str(uuid.uuid4()), now()
    item = {
        **body.model_dump(mode="json", exclude_none=True),
        "owner": owner,
        **task_key(m.org_id, pid, task_id),
        "entity_type": "task",
        "id": task_id,
        "project_id": pid,
        "approval_state": ApprovalState.draft.value,
        "created_by": m.caller.user_id,
        "created_at": ts,
        "updated_at": ts,
    }
    run_tx([tx_put(item),
            tx_audit(m.org_id, m.caller.user_id, "task.create", task_id, project_id=pid)], {})
    log_event("task.create", m.org_id, m.caller.user_id, resource_id=task_id, project_id=pid)
    return task_out(m, item)


@router.get("", response_model=TaskList)
def list_tasks(project_id: ProjectId, m: Member, limit: Limit = 50, cursor: Cursor = None):
    require(m, "task.read")
    pid = str(project_id)
    get_project_or_404(m.org_id, pid)
    page = query_page(m.org_id, task_prefix(pid), limit, cursor)
    log_event("task.list", m.org_id, m.caller.user_id, project_id=pid, count=len(page["items"]))
    return {"items": [task_out(m, i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@router.get("/{task_id}", response_model=TaskOut)
def get_task(project_id: ProjectId, task_id: TaskId, m: Member):
    require(m, "task.read")
    return task_out(m, get_task_or_404(m.org_id, str(project_id), str(task_id)))


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(project_id: ProjectId, task_id: TaskId, body: TaskUpdate, m: Member):
    pid, tid = str(project_id), str(task_id)
    task = get_task_or_404(m.org_id, pid, tid)
    require_task_access(m, task, "write")  # ownership check BEFORE the write
    if is_locked(current_state(task)):
        raise HTTPException(status_code=409, detail=LOCKED)
    fields = body.model_dump(mode="json", exclude_unset=True)
    if fields.get("owner"):
        fields["owner"] = resolve_owner(m.org_id, fields["owner"])
    changed = sorted(fields)
    fields["updated_at"] = now()
    expression, names, values = build_update(fields)
    condition = ("attribute_exists(PK)" + own_guard(m, "write", values, names)
                 + unlocked_guard(values, names))
    run_tx([
        tx_update(task_key(m.org_id, pid, tid), expression, condition, values, names),
        tx_audit(m.org_id, m.caller.user_id, "task.update", tid, project_id=pid, changed=changed),
    ], {0: (409, CHANGED_HANDS)})
    log_event("task.update", m.org_id, m.caller.user_id, resource_id=tid, project_id=pid,
              changed=changed)
    return task_out(m, get_task_or_404(m.org_id, pid, tid))


@router.delete("/{task_id}", status_code=204, response_class=Response)
def delete_task(project_id: ProjectId, task_id: TaskId, m: Member):
    pid, tid = str(project_id), str(task_id)
    task = get_task_or_404(m.org_id, pid, tid)
    require_task_access(m, task, "delete")  # ownership check BEFORE the delete
    if is_locked(current_state(task)):
        raise HTTPException(status_code=409, detail=LOCKED)
    values: dict = {}
    names: dict = {}
    condition = ("attribute_exists(PK)" + own_guard(m, "delete", values, names)
                 + unlocked_guard(values, names))
    run_tx([
        tx_delete(task_key(m.org_id, pid, tid), condition, values, names),
        tx_audit(m.org_id, m.caller.user_id, "task.delete", tid, project_id=pid),
    ], {0: (409, CHANGED_HANDS)})
    log_event("task.delete", m.org_id, m.caller.user_id, resource_id=tid, project_id=pid)
    return Response(status_code=204)