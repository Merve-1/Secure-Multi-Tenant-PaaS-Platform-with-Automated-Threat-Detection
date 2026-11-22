
from fastapi import FastAPI
from mangum import Mangum

app = FastAPI(
    title="Multi-Tenant SaaS API",
    description="Serverless SaaS platform API",
    version="1.0.0",
)

@app.get("/")
async def root():
    return {"message": "Welcome to the SaaS platform"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

handler = Mangum(app)
