import os 
from fastapi import FastAPI, HTTPException, Request
from mangum import Mangum

app = FastAPI(
    title="Multi-Tenant SaaS API",
    description="Serverless SaaS platform API",
    version="1.0.0",
)

TABLE_NAME = os.environ.get("TABLE_NAME", "")

def tenant_pk(tenant_id: str) -> str:
    """Every tenant-owned item lives under this partition key."""
    return f"TENANT#{tenant_id}"

def current_user(request: Request) -> dict:
    """Read the claims API Gateway already verified (signature, expiry, issuer, audience)."""
    event = request.scope.get("aws.event", {})
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_groups = claims.get("cognito:groups", "")
    groups = [g for g in raw_groups.strip("[]").replace(",", " ").split() if g]

    return{
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "tenant_id": claims.get("custom:tenant_id"),
        "groups": groups,
    }

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


handler = Mangum(app)

