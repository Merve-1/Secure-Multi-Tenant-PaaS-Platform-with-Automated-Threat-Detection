"""Projects, scoped to an organization: /orgs/{org_id}/projects

The caller's role comes from their membership in that organization. Each write
runs in one transaction together with its audit event.
"""
import uuid
from enum import Enum

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from core.auth import Member, require
from core.db import (PROJECT_PREFIX, build_update, get_project_or_404, has_items, now,
                     project_key, query_page, run_tx, task_prefix, tx_audit, tx_delete,
                     tx_put, tx_update)
from core.log import log_event
from core.schemas import Cursor, Limit, ProjectId, StrictModel

router = APIRouter(prefix="/orgs/{org_id}/projects", tags=["projects"])


class ProjectStatus(str, Enum):
    active = "active"
    archived = "archived"


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


class ProjectUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    status: ProjectStatus | None = None

    @model_validator(mode="after")
    def check_fields(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        for field in ("name", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    created_by: str
    created_at: str
    updated_at: str


class ProjectList(BaseModel):
    items: list[ProjectOut]
    next_cursor: str | None = None


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, m: Member):
    require(m, "project.write")
    project_id, ts = str(uuid.uuid4()), now()
    item = {
        **body.model_dump(mode="json", exclude_none=True),
        **project_key(m.org_id, project_id),
        "entity_type": "project",
        "id": project_id,
        "status": ProjectStatus.active.value,
        "created_by": m.caller.user_id,
        "created_at": ts,
        "updated_at": ts,
    }
    run_tx([tx_put(item),
            tx_audit(m.org_id, m.caller.user_id, "project.create", project_id)], {})
    log_event("project.create", m.org_id, m.caller.user_id, resource_id=project_id)
    return ProjectOut(**item)


@router.get("", response_model=ProjectList)
def list_projects(m: Member, limit: Limit = 50, cursor: Cursor = None):
    require(m, "project.read")
    page = query_page(m.org_id, PROJECT_PREFIX, limit, cursor)
    log_event("project.list", m.org_id, m.caller.user_id, count=len(page["items"]))
    return {"items": [ProjectOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: ProjectId, m: Member):
    require(m, "project.read")
    return ProjectOut(**get_project_or_404(m.org_id, str(project_id)))


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: ProjectId, body: ProjectUpdate, m: Member):
    require(m, "project.write")
    pid = str(project_id)
    fields = body.model_dump(mode="json", exclude_unset=True)
    changed = sorted(fields)
    fields["updated_at"] = now()
    expression, names, values = build_update(fields)
    run_tx([
        tx_update(project_key(m.org_id, pid), expression, "attribute_exists(PK)", values, names),
        tx_audit(m.org_id, m.caller.user_id, "project.update", pid, changed=changed),
    ], {0: (404, "Project not found")})
    log_event("project.update", m.org_id, m.caller.user_id, resource_id=pid, changed=changed)
    return ProjectOut(**get_project_or_404(m.org_id, pid))


@router.delete("/{project_id}", status_code=204, response_class=Response)
def delete_project(project_id: ProjectId, m: Member):
    require(m, "project.delete")
    pid = str(project_id)
    # Deleting a project with tasks would leave orphans, so refuse instead.
    # (Cascade deletes would need BatchWriteItem, which the IAM policy omits.)
    if has_items(m.org_id, task_prefix(pid)):
        raise HTTPException(status_code=409, detail="Project still has tasks. Delete them first.")
    run_tx([
        tx_delete(project_key(m.org_id, pid), "attribute_exists(PK)"),
        tx_audit(m.org_id, m.caller.user_id, "project.delete", pid),
    ], {0: (404, "Project not found")})
    log_event("project.delete", m.org_id, m.caller.user_id, resource_id=pid)
    return Response(status_code=204)