"""
Supervisor entrypoint.

Supervisor runs `uvicorn server:app` from /app/backend. We:
  1. Insert /app onto sys.path so the top-level `shared` package
     (/app/shared) is importable from backend code.
  2. Re-export `app` from app.main:app — the real FastAPI app.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent  # /app
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.main import app  # noqa: E402

__all__ = ["app"]
