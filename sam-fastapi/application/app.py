import os 
import logging
import re
import time
import uuid
import boto3
from mangum import Mangum
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from mangum import Mangum
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("saas")
logger.setLevel(logging.INFO)

app = FastAPI(
    title="Multi-Tenant SaaS API",
    description="Serverless SaaS platform API",
    version="1.0.0",
)

bearer_scheme = HTTPBearer(auto_error=False)

@lru_cache
def get_table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def tpk(tenant_id: str) -> str:
    """Partition key for everything a tenant owns."""
    return f"TENANT#{tenant_id}"


def project_sk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def task_sk(project_id: str, task_id: str) -> str:
    return f"TASK#{project_id}#{task_id}"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def build_update(fields: dict[str, Any]):
    """Build an UpdateExpression. Every attribute name is aliased because words
    such as 'name' and 'status' are reserved in DynamoDB. None means REMOVE."""
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


# Authentication and authorization
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Role -> allowed actions. Anything not listed is denied.
ROLE_PERMISSIONS = {
    "admin": {"read", "write", "delete"},
    "member": {"read", "write"},
    "viewer": {"read"},
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    tenant_id: str
    groups: list[str]


def get_principal(
    request: Request,
    _credentials: Annotated[Any, Depends(bearer_scheme)],
) -> Principal:
    """Read the claims API Gateway has already verified (signature, expiry,
    issuer, audience)."""
    event = request.scope.get("aws.event", {})
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tenant_id = claims.get("custom:tenant_id")
    if not tenant_id or not TENANT_RE.match(tenant_id):
        logger.warning(
            "access_denied",
            extra={"action": "auth.tenant", "outcome": "denied", "user_id": user_id,
                   "reason": "missing_or_invalid_tenant"},
        )
        raise HTTPException(status_code=403, detail="Forbidden")

    # HTTP API passes list claims as a string such as "[admin member]"
    raw_groups = claims.get("cognito:groups", "")
    groups = [g for g in raw_groups.strip("[]").replace(",", " ").split() if g]

    return Principal(user_id=user_id, email=claims.get("email"),
                     tenant_id=tenant_id, groups=groups)


def needs(action: str):
    """Dependency factory: require a role that allows `action`."""
    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        allowed = any(action in ROLE_PERMISSIONS.get(g, ()) for g in principal.groups)
        if not allowed:
            logger.warning(
                "access_denied",
                extra={"action": f"authz.{action}", "outcome": "denied",
                       "tenant_id": principal.tenant_id, "user_id": principal.user_id,
                       "groups": principal.groups},
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        return principal
    return dependency


Reader = Annotated[Principal, Depends(needs("read"))]
Writer = Annotated[Principal, Depends(needs("write"))]
Deleter = Annotated[Principal, Depends(needs("delete"))]


def log_event(action: str, p: Principal, **fields: Any) -> None:
    """One structured log line per business operation. Never pass tokens,
    passwords or request bodies here."""
    logger.info(
        action,
        extra={"action": action, "outcome": "success",
               "tenant_id": p.tenant_id, "user_id": p.user_id, **fields},
    )

# Request and response models (strict: unknown fields are rejected)
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectStatus(str, Enum):
    active = "active"
    archived = "archived"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


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


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: ProjectStatus
    created_by: str
    created_at: str
    updated_at: str


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


class ProjectList(BaseModel):
    items: list[ProjectOut]
    next_cursor: str | None = None


class TaskList(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None = None


ProjectId = Annotated[UUID, Path(description="Project ID (UUID)")]
TaskId = Annotated[UUID, Path(description="Task ID (UUID)")]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=200)]

# Error handling and request logging
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Report field names and messages only. Never echo the submitted values.
    errors = [
        {"field": ".".join(str(part) for part in e["loc"] if part != "body"),
         "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422,
                        content={"detail": "Validation failed", "errors": errors})


@app.exception_handler(ClientError)
async def aws_error_handler(request: Request, exc: ClientError):
    logger.error(
        "aws_error",
        extra={"error_code": exc.response["Error"]["Code"],
               "operation": exc.operation_name},
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def request_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    logger.info(
        "request",
        extra={"method": request.method, "path": request.url.path,
               "status": response.status_code,
               "duration_ms": round((time.perf_counter() - start) * 1000)},
    )
    return response

# General endpoints
@app.get("/")
async def root():
    return {"message": "Welcome to the SaaS platform"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/hello")
async def hello(request: Request):
    user = current_user(request)
    if not user["tenant_id"]:
        raise HTTPException(status_code=403, detail="No tenant assigned")
    return{
        "message": f"Hello {user['email']}",
        "tenant_id": user["tenant_id"],
        "groups": user["groups"],
        "partition_key": tenant_pk(user["tenant_id"]),
    }


# Shared query helpers
def fetch_project(p: Principal, project_id: str) -> dict:
    resp = get_table().get_item(
        Key={"PK": tpk(p.tenant_id), "SK": project_sk(project_id)},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        # Same answer for "does not exist" and "belongs to another tenant".
        raise HTTPException(status_code=404, detail="Project not found")
    return item


def query_page(p: Principal, prefix: str, limit: int, cursor: str | None) -> dict:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression":
            Key("PK").eq(tpk(p.tenant_id)) & Key("SK").begins_with(prefix),
        "Limit": limit,
        "ConsistentRead": True,
    }
    if cursor:
        if not cursor.startswith(prefix):
            raise HTTPException(status_code=422, detail="Invalid cursor")
        # Only the sort key comes from the client; the partition key is rebuilt
        # here from the verified tenant ID.
        kwargs["ExclusiveStartKey"] = {"PK": tpk(p.tenant_id), "SK": cursor}
    resp = get_table().query(**kwargs)
    last = resp.get("LastEvaluatedKey")
    return {"items": resp["Items"], "next_cursor": last["SK"] if last else None}


def apply_update(p: Principal, sk: str, fields: dict[str, Any], not_found: str) -> dict:
    fields["updated_at"] = utcnow()
    expression, names, values = build_update(fields)
    try:
        resp = get_table().update_item(
            Key={"PK": tpk(p.tenant_id), "SK": sk},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if is_conditional_failure(exc):
            raise HTTPException(status_code=404, detail=not_found)
        raise
    return resp["Attributes"]


def delete_existing(p: Principal, sk: str, not_found: str) -> None:
    try:
        get_table().delete_item(
            Key={"PK": tpk(p.tenant_id), "SK": sk},
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as exc:
        if is_conditional_failure(exc):
            raise HTTPException(status_code=404, detail=not_found)
        raise


# Projects
@app.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, p: Writer):
    project_id = str(uuid.uuid4())
    now = utcnow()
    data = body.model_dump(mode="json", exclude_none=True)
    item = {
        **data,
        "PK": tpk(p.tenant_id),
        "SK": project_sk(project_id),
        "entity_type": "project",
        "id": project_id,
        "status": ProjectStatus.active.value,
        "created_by": p.user_id,
        "created_at": now,
        "updated_at": now,
    }
    get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    log_event("project.create", p, resource_id=project_id)
    return ProjectOut(**item)


@app.get("/projects", response_model=ProjectList)
def list_projects(p: Reader, limit: Limit = 50, cursor: Cursor = None):
    page = query_page(p, "PROJECT#", limit, cursor)
    log_event("project.list", p, count=len(page["items"]))
    return {"items": [ProjectOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@app.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: ProjectId, p: Reader):
    item = fetch_project(p, str(project_id))
    return ProjectOut(**item)


@app.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: ProjectId, body: ProjectUpdate, p: Writer):
    fields = body.model_dump(mode="json", exclude_unset=True)
    item = apply_update(p, project_sk(str(project_id)), fields, "Project not found")
    log_event("project.update", p, resource_id=str(project_id),
              changed=sorted(k for k in fields if k != "updated_at"))
    return ProjectOut(**item)


@app.delete("/projects/{project_id}", status_code=204, response_class=Response)
def delete_project(project_id: ProjectId, p: Deleter):
    pid = str(project_id)
    # Deleting a project with tasks would leave orphans, so refuse instead.
    # (Cascade deletes would need BatchWriteItem, which the IAM policy omits.)
    tasks = get_table().query(
        KeyConditionExpression=Key("PK").eq(tpk(p.tenant_id))
        & Key("SK").begins_with(f"TASK#{pid}#"),
        Limit=1,
        ConsistentRead=True,
    )
    if tasks["Items"]:
        raise HTTPException(status_code=409,
                            detail="Project still has tasks. Delete them first.")
    delete_existing(p, project_sk(pid), "Project not found")
    log_event("project.delete", p, resource_id=pid)
    return Response(status_code=204)

# Tasks (always scoped to a project in the caller's tenant)
@app.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
def create_task(project_id: ProjectId, body: TaskCreate, p: Writer):
    pid = str(project_id)
    fetch_project(p, pid)  # 404 if the project is missing or another tenant's
    task_id = str(uuid.uuid4())
    now = utcnow()
    data = body.model_dump(mode="json", exclude_none=True)
    item = {
        "owner": p.email or p.user_id,
        **data,
        "PK": tpk(p.tenant_id),
        "SK": task_sk(pid, task_id),
        "entity_type": "task",
        "id": task_id,
        "project_id": pid,
        "created_by": p.user_id,
        "created_at": now,
        "updated_at": now,
    }
    get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    log_event("task.create", p, resource_id=task_id, project_id=pid)
    return TaskOut(**item)


@app.get("/projects/{project_id}/tasks", response_model=TaskList)
def list_tasks(project_id: ProjectId, p: Reader, limit: Limit = 50, cursor: Cursor = None):
    pid = str(project_id)
    fetch_project(p, pid)
    page = query_page(p, f"TASK#{pid}#", limit, cursor)
    log_event("task.list", p, project_id=pid, count=len(page["items"]))
    return {"items": [TaskOut(**i) for i in page["items"]],
            "next_cursor": page["next_cursor"]}


@app.get("/projects/{project_id}/tasks/{task_id}", response_model=TaskOut)
def get_task(project_id: ProjectId, task_id: TaskId, p: Reader):
    resp = get_table().get_item(
        Key={"PK": tpk(p.tenant_id), "SK": task_sk(str(project_id), str(task_id))},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskOut(**item)


@app.patch("/projects/{project_id}/tasks/{task_id}", response_model=TaskOut)
def update_task(project_id: ProjectId, task_id: TaskId, body: TaskUpdate, p: Writer):
    fields = body.model_dump(mode="json", exclude_unset=True)
    item = apply_update(p, task_sk(str(project_id), str(task_id)), fields,
                        "Task not found")
    log_event("task.update", p, resource_id=str(task_id), project_id=str(project_id),
              changed=sorted(k for k in fields if k != "updated_at"))
    return TaskOut(**item)


@app.delete("/projects/{project_id}/tasks/{task_id}", status_code=204,
            response_class=Response)
def delete_task(project_id: ProjectId, task_id: TaskId, p: Deleter):
    delete_existing(p, task_sk(str(project_id), str(task_id)), "Task not found")
    log_event("task.delete", p, resource_id=str(task_id), project_id=str(project_id))
    return Response(status_code=204)




handler = Mangum(app)

