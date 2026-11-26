"""Tasks, scoped to a project in an organization:
/orgs/{org_id}/projects/{project_id}/tasks
"""
import uuid
from datetime import date
from enum import Enum

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from core.auth import Member, require
from core.db import (build_update, get_item, get_project_or_404, now, query_page, run_tx,
                     task_key, task_prefix, tx_audit, tx_delete, tx_put, tx_update)
from core.log import log_event
from core.schemas import Cursor, Limit, ProjectId, StrictModel, TaskId

router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/tasks", tags=["tasks"])


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class TaskCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, min_length=1, max_length=100)
    status: TaskStatus = TaskStatus.todo
    due_date: date | None = None


class TaskUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, min_length=1, max_length=100)
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


class TaskList(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None = None


def get_task_or_404(org_id: str, project_id: str, task_id: str) -> dict:
    item = get_item(task_key(org_id, project_id, task_id))
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")
    return item


@router.post("", response_model=TaskOut, status_code=201)
def create_task(project_id: ProjectId, body: TaskCreate, m: Member):
    require(m, "task.write")
    pid = str(project_id)
    get_project_or_404(m.org_id, pid)  # 404 if missing or in another organization
    task_id, ts = str(uuid.uuid4()), now()
    item = {
        "owner": m.caller.email,
        **body.model_dump(mode="json", exclude_none=True),
        **task_key(m.org_id, pid, task_id),
        "entity_type": "task",
        "id": task_id,
        "project_id": pid,
        "created_by": m.caller.user_id,
        "created_at": ts,
        "updated_at": ts,
    }
    run_tx([tx_put(item),
            tx_audit(m.org_id, m.caller.user_id, "task.create", task_id, project_id=pid)], {})
    log_event("task.create", m.org_id, m.caller.user_id, resource_id=task_id, project_id=pid)
    return TaskOut(**item)


@router.get("", response_model=TaskList)
def list_tasks(project_id: ProjectId, m: Member, limit: Limit = 50, cursor: Cursor = None):
    require(m, "task.read")
    pid = str(project_id)
    get_project_or_404(m.org_id, pid)
    page = query_page(m.org_id, task_prefix(pid), limit, cursor)
    log_event("task.list", m.org_id, m.caller.user_id, project_id=pid, count=len(page["items"]))
    return {"items": [TaskOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@router.get("/{task_id}", response_model=TaskOut)
def get_task(project_id: ProjectId, task_id: TaskId, m: Member):
    require(m, "task.read")
    return TaskOut(**get_task_or_404(m.org_id, str(project_id), str(task_id)))


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(project_id: ProjectId, task_id: TaskId, body: TaskUpdate, m: Member):
    require(m, "task.write")
    pid, tid = str(project_id), str(task_id)
    fields = body.model_dump(mode="json", exclude_unset=True)
    changed = sorted(fields)
    fields["updated_at"] = now()
    expression, names, values = build_update(fields)
    run_tx([
        tx_update(task_key(m.org_id, pid, tid), expression, "attribute_exists(PK)", values, names),
        tx_audit(m.org_id, m.caller.user_id, "task.update", tid, project_id=pid, changed=changed),
    ], {0: (404, "Task not found")})
    log_event("task.update", m.org_id, m.caller.user_id, resource_id=tid, project_id=pid,
              changed=changed)
    return TaskOut(**get_task_or_404(m.org_id, pid, tid))


@router.delete("/{task_id}", status_code=204, response_class=Response)
def delete_task(project_id: ProjectId, task_id: TaskId, m: Member):
    require(m, "task.delete")
    pid, tid = str(project_id), str(task_id)
    run_tx([
        tx_delete(task_key(m.org_id, pid, tid), "attribute_exists(PK)"),
        tx_audit(m.org_id, m.caller.user_id, "task.delete", tid, project_id=pid),
    ], {0: (404, "Task not found")})
    log_event("task.delete", m.org_id, m.caller.user_id, resource_id=tid, project_id=pid)
    return Response(status_code=204)
