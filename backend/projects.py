"""
Project management routes.
"""

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str

class ProjectRename(BaseModel):
    name: str


@router.get("")
def list_projects(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name FROM projects WHERE user_id=? ORDER BY id ASC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
def create_project(body: ProjectCreate, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    conn = get_db()
    with conn:
        cur     = conn.execute("INSERT INTO projects (name, user_id) VALUES (?,?)", (name, user["id"]))
        proj_id = cur.lastrowid
        conn.commit()
    conn.close()
    return {"id": proj_id, "name": name}


@router.put("/{project_id}")
def rename_project(project_id: int, body: ProjectRename, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    conn = get_db()
    with conn:
        n = conn.execute(
            "UPDATE projects SET name=? WHERE id=? AND user_id=?", (name, project_id, user["id"])
        ).rowcount
        conn.commit()
    conn.close()
    if not n:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"id": project_id, "name": name}


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    with conn:
        conn.execute("UPDATE papers SET project_id=NULL WHERE project_id=? AND user_id=?", (project_id, user["id"]))
        conn.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user["id"]))
        conn.commit()
    conn.close()
    return Response(status_code=204)
