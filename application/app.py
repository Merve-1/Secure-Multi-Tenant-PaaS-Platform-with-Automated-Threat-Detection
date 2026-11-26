"""Application entry point.

Creates the FastAPI app, registers the routers, and wires up error handling and
request logging. Business logic lives in routers/, shared code in core/.
Import direction is one way: app -> routers -> core.
"""
import time

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum

from core.auth import CallerDep
from core.log import logger
from routers import orgs, projects, tasks

app = FastAPI(
    title="Multi-Tenant SaaS API",
    description="Serverless SaaS platform API",
    version="2.0.0",
)

app.include_router(orgs.router)
app.include_router(projects.router)
app.include_router(tasks.router)


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------
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
    logger.error("aws_error", extra={"error_code": exc.response["Error"]["Code"],
                                     "operation": exc.operation_name})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# Request logging and response headers
# --------------------------------------------------------------------------
@app.middleware("http")
async def request_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    logger.info("request", extra={
        "method": request.method, "path": request.url.path,
        "status": response.status_code,
        "duration_ms": round((time.perf_counter() - start) * 1000)})
    return response


# --------------------------------------------------------------------------
# General endpoints
# --------------------------------------------------------------------------
@app.get("/")
def root():
    return {"message": "Welcome to the SaaS platform"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/me")
def me(caller: CallerDep):
    """Who the API thinks you are. Use GET /orgs for your organizations."""
    return {"user_id": caller.user_id, "email": caller.email}


handler = Mangum(app, lifespan="off")