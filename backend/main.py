"""
OGAI Annotation Platform — FastAPI backend
v2.0 — modular architecture, optimistic concurrency, input validation, security hardening
"""

from fastapi import Cookie, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import get_user_from_token
from .config import FRONTEND
from .db import init_db

# Run schema migrations on startup
init_db()

# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform")


# ─────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root(ogai_session: str | None = Cookie(default=None)):
    user = get_user_from_token(ogai_session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(str(FRONTEND / "annotator.html"), media_type="text/html")


@app.get("/login", include_in_schema=False)
def login_page(ogai_session: str | None = Cookie(default=None)):
    user = get_user_from_token(ogai_session)
    if user:
        return RedirectResponse("/", status_code=302)
    return FileResponse(str(FRONTEND / "login.html"), media_type="text/html")


# ─────────────────────────────────────────────
# Static files
# ─────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="frontend")


# ─────────────────────────────────────────────
# Register API routers
# ─────────────────────────────────────────────
from .auth import router as auth_router
from .projects import router as projects_router
from .papers import router as papers_router
from .annotations import router as annotations_router
from .prefill import router as prefill_router
from .export import router as export_router
from .admin import router as admin_router

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(papers_router)
app.include_router(annotations_router)
app.include_router(prefill_router)
app.include_router(export_router)
app.include_router(admin_router)
