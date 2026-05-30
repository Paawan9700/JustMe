"""
FastAPI application entrypoint.

Milestone 0 exposes a single /health endpoint that verifies both
MongoDB and Cloudflare R2 are reachable.

Routing note: Emergent's ingress only forwards paths prefixed with /api
to this service. The health check is therefore mounted at /api/health
(externally reachable) and also at /health (for local curl during dev).
"""

from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.jobs import router as jobs_router
from app.db.mongo import close_db, init_db, ping as mongo_ping
from app.services.storage import get_storage


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Ensure indexes on startup.
    await init_db()
    yield
    await close_db()


app = FastAPI(title="JustMe API", version="0.1.0", lifespan=lifespan)

# CORS: the React frontend is served from a different origin in dev/preview,
# so allow it to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _health_payload() -> dict:
    """Probe Mongo and R2 in parallel and build the /health response."""
    storage = get_storage()

    # R2 ping is a sync boto3 call; run it in a thread so we don't block.
    async def _r2_ping() -> bool:
        return await anyio.to_thread.run_sync(storage.ping)

    async with anyio.create_task_group() as tg:
        results: dict[str, bool] = {}

        async def _do_mongo():
            results["mongo"] = await mongo_ping()

        async def _do_r2():
            results["r2"] = await _r2_ping()

        tg.start_soon(_do_mongo)
        tg.start_soon(_do_r2)

    mongo_ok = results["mongo"]
    r2_ok = results["r2"]

    return {
        "status": "ok" if (mongo_ok and r2_ok) else "degraded",
        "mongo": "connected" if mongo_ok else "disconnected",
        "r2": "connected" if r2_ok else "disconnected",
    }


@app.get("/health")
async def health_root():
    """Health check — local/dev access (curl http://localhost:8001/health)."""
    return await _health_payload()


@app.get("/api/health")
async def health_api():
    """Health check — reachable through Emergent ingress."""
    return await _health_payload()


@app.get("/api")
async def api_root():
    """Cheap liveness probe for the /api prefix."""
    return {"service": "JustMe API", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(jobs_router)
