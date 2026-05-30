"""
Supervisor entrypoint.

Supervisor runs `uvicorn server:app` from /app/backend. The real FastAPI
app lives at app.main:app — this module just re-exports it so the spec's
folder structure (backend/app/main.py) is preserved.
"""

from app.main import app

__all__ = ["app"]
