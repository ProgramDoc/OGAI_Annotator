"""
Admin routes: schema reset, DB debug info.
"""

import os

from fastapi import APIRouter, Cookie, HTTPException

from .auth import require_user
from .config import DB_PATH, PAPERS_DIR
from .db import get_db, init_db

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.api_route("/reset-schema", methods=["GET", "POST"])
def reset_schema(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    init_db()
    conn = get_db()
    deleted = conn.execute("DELETE FROM papers WHERE user_id IS NULL").rowcount
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Schema migrations re-applied; {deleted} orphan rows deleted"}


@router.get("/debug-db")
def debug_db(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    papers = conn.execute("SELECT id, filename, user_id, sha256, disk_filename FROM papers ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    disk_files = sorted(str(f.name) for f in PAPERS_DIR.iterdir()) if PAPERS_DIR.exists() else []
    return {
        "DB_PATH":     str(DB_PATH),
        "DB_exists":   DB_PATH.exists(),
        "PAPERS_DIR":  str(PAPERS_DIR),
        "papers_dir_exists": PAPERS_DIR.exists(),
        "disk_files":  disk_files[:30],
        "paper_rows":  [dict(r) for r in papers],
        "RENDER_DATA_DIR_env": os.environ.get("RENDER_DATA_DIR", "NOT SET"),
    }
