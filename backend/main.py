"""
OGAI Annotation Platform — FastAPI backend
v1.4 — user auth (register / login / sessions), admin secret, user-scoped data
"""

import asyncio
import logging
import traceback
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Cookie, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("ogai")

# ─────────────────────────────────────────────
# Paths & config
# ─────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", BASE_DIR))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
FRONTEND   = BASE_DIR / "frontend"

PAPERS_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL",  "admin@ogai.local")
ADMIN_NAME   = os.environ.get("ADMIN_NAME",   "Admin")

SESSION_COOKIE = "ogai_session"
SESSION_DAYS   = 30
PBKDF2_ITERS   = 260_000


# ─────────────────────────────────────────────
# Password hashing  (stdlib only)
# ─────────────────────────────────────────────
def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
    return dk.hex(), salt.hex()


def _verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    salt = bytes.fromhex(stored_salt)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
    return hmac.compare_digest(dk.hex(), stored_hash)


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_db()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                display_name  TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                password_salt TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'reviewer',
                created_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT    DEFAULT (datetime('now')),
                expires_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS papers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT    NOT NULL,
                sha256     TEXT    NOT NULL,
                user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                created_at TEXT    DEFAULT (datetime('now')),
                UNIQUE(sha256, user_id)
            );

            CREATE TABLE IF NOT EXISTS annotations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                reviewer_id TEXT    NOT NULL,
                data_json   TEXT    DEFAULT '{}',
                timestamp   TEXT    DEFAULT (datetime('now')),
                correction_notes          TEXT,
                corrections_json          TEXT,
                pipeline_predictions_json TEXT,
                UNIQUE(paper_id, reviewer_id)
            );

            CREATE TABLE IF NOT EXISTS spans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                reviewer_id TEXT    NOT NULL,
                field_name  TEXT    NOT NULL,
                page        INTEGER,
                text        TEXT,
                x0 REAL, y0 REAL, x1 REAL, y1 REAL
            );
        """)

        for migration in [
            "ALTER TABLE papers      ADD COLUMN sha256        TEXT",
            "ALTER TABLE papers      ADD COLUMN user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE",
            "ALTER TABLE papers      ADD COLUMN project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL",
            "ALTER TABLE papers      ADD COLUMN created_at    TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE papers      ADD COLUMN disk_filename TEXT",
            "ALTER TABLE projects    ADD COLUMN user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE",
            "ALTER TABLE annotations ADD COLUMN correction_notes          TEXT",
            "ALTER TABLE annotations ADD COLUMN corrections_json          TEXT",
            "ALTER TABLE annotations ADD COLUMN pipeline_predictions_json TEXT",
            "ALTER TABLE annotations ADD COLUMN field_annotations_json    TEXT",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass

        # Backfill NULL ids for legacy TEXT PRIMARY KEY schema
        try:
            conn.execute("UPDATE papers SET id=CAST(rowid AS TEXT) WHERE id IS NULL OR id=''")
        except Exception:
            pass

        conn.commit()
    conn.close()
    _ensure_admin_user()


def _ensure_admin_user() -> None:
    if not ADMIN_SECRET:
        return
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if not existing:
        ph, ps = _hash_password(ADMIN_SECRET)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (email, display_name, password_hash, password_salt, role) VALUES (?,?,?,?,?)",
                (ADMIN_EMAIL, ADMIN_NAME, ph, ps, "admin"),
            )
            conn.commit()
    conn.close()


init_db()

# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform")


# ─────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────
def _create_session(user_id: int) -> str:
    token   = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires),
        )
        conn.commit()
    conn.close()
    return token


def _get_user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    row  = conn.execute(
        """SELECT u.id, u.email, u.display_name, u.role
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND s.expires_at > ?""",
        (token, now),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def require_user(ogai_session: str | None = Cookie(default=None)) -> dict:
    user = _get_user_from_token(ogai_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=bool(os.environ.get("RENDER")),
        max_age=SESSION_DAYS * 86400,
        path="/",
    )


# ─────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────
class RegisterPayload(BaseModel):
    email: str
    password: str
    display_name: str

class LoginPayload(BaseModel):
    email: str
    password: str

class AdminLoginPayload(BaseModel):
    secret: str

class ProjectCreate(BaseModel):
    name: str

class ProjectRename(BaseModel):
    name: str

class PaperAssign(BaseModel):
    project_id: Optional[int] = None

class AnnotationPayload(BaseModel):
    data: dict[str, Any] = {}
    spans: list[dict[str, Any]] = []
    field_annotations: dict[str, Any] = {}

class PrefillRequest(BaseModel):
    study_type: str


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root(ogai_session: str | None = Cookie(default=None)):
    user = _get_user_from_token(ogai_session)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(str(FRONTEND / "annotator.html"), media_type="text/html")


@app.get("/login", include_in_schema=False)
def login_page(ogai_session: str | None = Cookie(default=None)):
    user = _get_user_from_token(ogai_session)
    if user:
        return RedirectResponse("/", status_code=302)
    return FileResponse(str(FRONTEND / "login.html"), media_type="text/html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="frontend")


# ─────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────
@app.post("/api/auth/register", status_code=201)
def register(body: RegisterPayload):
    email    = body.email.strip().lower()
    name     = body.display_name.strip()
    password = body.password
    if not email or not name or not password:
        raise HTTPException(status_code=422, detail="All fields are required")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email address")

    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    ph, ps = _hash_password(password)
    with conn:
        conn.execute(
            "INSERT INTO users (email, display_name, password_hash, password_salt) VALUES (?,?,?,?)",
            (email, name, ph, ps),
        )
        conn.commit()
    user = conn.execute("SELECT id, email, display_name FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(user)


@app.post("/api/auth/login")
def login(body: LoginPayload):
    email = body.email.strip().lower()
    conn  = get_db()
    user  = conn.execute(
        "SELECT id, email, display_name, password_hash, password_salt, role FROM users WHERE email=?",
        (email,),
    ).fetchone()
    conn.close()

    if not user or not _verify_password(body.password, user["password_hash"], user["password_salt"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token    = _create_session(user["id"])
    response = Response(
        content=json.dumps({
            "id": user["id"], "email": user["email"],
            "display_name": user["display_name"], "role": user["role"],
        }),
        media_type="application/json",
    )
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/admin")
def admin_login(body: AdminLoginPayload):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if not hmac.compare_digest(body.secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    _ensure_admin_user()
    conn = get_db()
    user = conn.execute(
        "SELECT id, email, display_name, role FROM users WHERE email=?", (ADMIN_EMAIL,)
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=500, detail="Admin user could not be initialised")

    token    = _create_session(user["id"])
    response = Response(
        content=json.dumps({
            "id": user["id"], "email": user["email"],
            "display_name": user["display_name"], "role": user["role"],
        }),
        media_type="application/json",
    )
    _set_session_cookie(response, token)
    return response


@app.post("/api/auth/logout")
def logout(ogai_session: str | None = Cookie(default=None)):
    if ogai_session:
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (ogai_session,))
            conn.commit()
        conn.close()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def me(ogai_session: str | None = Cookie(default=None)):
    user = _get_user_from_token(ogai_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ─────────────────────────────────────────────
# Projects
# ─────────────────────────────────────────────
@app.get("/api/projects")
def list_projects(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name FROM projects WHERE user_id=? ORDER BY id ASC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects", status_code=201)
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


@app.put("/api/projects/{project_id}")
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


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    with conn:
        conn.execute("UPDATE papers SET project_id=NULL WHERE project_id=? AND user_id=?", (project_id, user["id"]))
        conn.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user["id"]))
        conn.commit()
    conn.close()
    return Response(status_code=204)


# ─────────────────────────────────────────────
# Papers
# ─────────────────────────────────────────────
@app.get("/api/papers")
def list_papers(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    papers = conn.execute(
        "SELECT rowid as rid, filename, project_id FROM papers WHERE user_id=? ORDER BY rowid DESC",
        (user["id"],),
    ).fetchall()
    result = []
    for p in papers:
        reviewers = conn.execute(
            "SELECT reviewer_id FROM annotations WHERE paper_id=?", (p["rid"],)
        ).fetchall()
        result.append({
            "id":           p["rid"],
            "filename":     p["filename"],
            "project_id":   p["project_id"],
            "annotated_by": [r["reviewer_id"] for r in reviewers],
        })
    conn.close()
    return result


def _papers_columns() -> set[str]:
    """Return the set of column names that currently exist in the papers table."""
    conn = get_db()
    try:
        rows = conn.execute("PRAGMA table_info(papers)").fetchall()
        return {row["name"] for row in rows}
    except Exception:
        return set()
    finally:
        conn.close()


@app.post("/api/papers/upload", status_code=201)
async def upload_paper(file: UploadFile = File(...), ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    data = await file.read()
    sha  = hashlib.sha256(data).hexdigest()
    uid  = user["id"]

    # ── Step 1: write PDF bytes to disk ─────────────────────────────────
    dest = PAPERS_DIR / f"{sha[:16]}_{uid}.pdf"
    if not dest.exists():
        legacy = PAPERS_DIR / f"{sha[:16]}.pdf"
        dest.write_bytes(legacy.read_bytes() if legacy.exists() else data)
    disk_fn = dest.name

    # ── Step 2: discover actual DB columns + NOT NULL constraints ──────
    pragma_conn = get_db()
    pragma_rows = pragma_conn.execute("PRAGMA table_info(papers)").fetchall()
    pragma_conn.close()
    col_info = {r["name"]: {"notnull": r["notnull"], "dflt": r["dflt_value"]} for r in pragma_rows}
    cols = set(col_info.keys())
    logger.error(f"upload: schema = {[(n, col_info[n]) for n in sorted(cols)]}")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Known legacy NOT NULL columns and sensible fill values
    LEGACY_DEFAULTS = {
        "file_path":   disk_fn,
        "upload_time": now_str,
        "created_at":  now_str,
        "timestamp":   now_str,
    }

    conn = get_db()
    try:
        # ── Step 3: build INSERT supplying ALL NOT NULL / no-default cols ──
        insert_cols = ["filename"]
        insert_vals = [file.filename]
        if "sha256"        in cols: insert_cols.append("sha256");        insert_vals.append(sha)
        if "user_id"       in cols: insert_cols.append("user_id");       insert_vals.append(uid)
        if "disk_filename" in cols: insert_cols.append("disk_filename"); insert_vals.append(disk_fn)

        # Add any other NOT NULL / no-default columns using legacy fallbacks
        for col, info in col_info.items():
            if col in insert_cols or col == "id":
                continue
            if info["notnull"] and info["dflt"] is None:
                fallback = LEGACY_DEFAULTS.get(col, "")
                insert_cols.append(col)
                insert_vals.append(fallback)
                logger.error(f"upload: adding NOT NULL col '{col}' = '{fallback}'")

        placeholders = ",".join("?" * len(insert_cols))
        insert_sql   = f"INSERT OR IGNORE INTO papers ({','.join(insert_cols)}) VALUES ({placeholders})"
        logger.error(f"upload: sql={insert_sql} params={insert_vals[:5]}")

        cur = conn.execute(insert_sql, insert_vals)
        conn.commit()
        rowid = cur.lastrowid
        logger.error(f"upload: INSERT rowid={rowid}")

        # ── Step 4: backfill id=rowid (legacy TEXT PRIMARY KEY schema) ───
        try:
            conn.execute(
                "UPDATE papers SET id=CAST(rowid AS TEXT) WHERE rowid=? AND (id IS NULL OR id='')",
                (rowid,)
            )
        except Exception as e:
            logger.error(f"upload: id backfill (ok to ignore): {e}")
        if "user_id" in cols:
            conn.execute("UPDATE papers SET user_id=? WHERE rowid=?", (uid, rowid))
        if "disk_filename" in cols:
            conn.execute("UPDATE papers SET disk_filename=? WHERE rowid=?", (disk_fn, rowid))
        conn.execute("UPDATE papers SET filename=? WHERE rowid=?", (file.filename, rowid))
        conn.commit()

        # ── Step 5: SELECT by rowid — always works regardless of id type ─
        sel_cols = ["filename"]
        if "project_id" in cols: sel_cols.append("project_id")
        row = conn.execute(
            f"SELECT {','.join(sel_cols)}, rowid FROM papers WHERE rowid=?", (rowid,)
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=500,
                detail=f"Upload failed: rowid={rowid} not found after insert")

        proj_id = row["project_id"] if "project_id" in cols else None
        logger.error(f"upload: success rowid={rowid}")
        return {"id": rowid, "filename": file.filename, "project_id": proj_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"upload: unhandled: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Upload error: {str(e)}")
    finally:
        conn.close()


@app.get("/api/papers/{paper_id}/pdf")
def get_pdf(paper_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()

    # Fetch all potentially useful columns, tolerating missing ones
    cols = _papers_columns()
    sel  = ["sha256", "user_id", "disk_filename"]
    if "file_path" in cols: sel.append("file_path")

    logger.error(f"get_pdf: looking up id={paper_id} DB={DB_PATH} exists={DB_PATH.exists()}")

    all_ids = conn.execute("SELECT id, user_id FROM papers ORDER BY id DESC LIMIT 5").fetchall()
    logger.error(f"get_pdf: last 5 rows in papers = {[(r['id'], r['user_id']) for r in all_ids]}")

    row  = conn.execute(
        f"SELECT {','.join(sel)} FROM papers WHERE id=?", (paper_id,)
    ).fetchone()
    conn.close()

    logger.error(f"get_pdf: row={dict(row) if row else None}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found in DB at {DB_PATH}")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail=f"Access denied: row uid={row['user_id']} user uid={user['id']}")

    sha      = row["sha256"] or ""
    uid      = row["user_id"] or 0
    disk_fn  = row["disk_filename"] or ""
    file_path = row["file_path"] if "file_path" in cols else ""

    # Build candidate list — most specific first
    candidates = []
    if disk_fn:
        candidates.append(PAPERS_DIR / disk_fn)
    if file_path:
        # file_path may be just a filename or a full path — try both
        candidates.append(PAPERS_DIR / Path(file_path).name)
        candidates.append(Path(file_path))
    if sha:
        candidates += [
            PAPERS_DIR / f"{sha[:16]}_{uid}.pdf",
            PAPERS_DIR / f"{sha[:16]}.pdf",
        ]

    logger.error(f"get_pdf: paper_id={paper_id} sha={sha[:8]} candidates={[str(c) for c in candidates]}")

    for c in candidates:
        if c.exists():
            logger.error(f"get_pdf: serving {c}")
            return FileResponse(str(c), media_type="application/pdf")

    logger.error(f"get_pdf: 404 — none of {[str(c) for c in candidates]} exist. PAPERS_DIR={PAPERS_DIR} contents={list(PAPERS_DIR.iterdir())[:10]}")
    raise HTTPException(status_code=404, detail="PDF file missing from disk")


@app.delete("/api/papers/{paper_id}", status_code=204)
def delete_paper(paper_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    row  = conn.execute("SELECT sha256, user_id, disk_filename FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Paper not found")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")
    sha = row["sha256"] or ""
    uid = row["user_id"] or 0
    disk_fn = row["disk_filename"]
    conn.execute("DELETE FROM spans       WHERE paper_id=?", (paper_id,))
    conn.execute("DELETE FROM annotations WHERE paper_id=?", (paper_id,))
    conn.execute("DELETE FROM papers      WHERE rowid=?",    (paper_id,))
    conn.commit()
    conn.close()
    candidates = []
    if disk_fn:
        candidates.append(PAPERS_DIR / disk_fn)
    if sha:
        candidates += [PAPERS_DIR / f"{sha[:16]}_{uid}.pdf", PAPERS_DIR / f"{sha[:16]}.pdf"]
    for c in candidates:
        if c.exists():
            c.unlink()
            break
    return Response(status_code=204)


@app.post("/api/papers/{paper_id}/assign")
def assign_paper(paper_id: int, body: PaperAssign, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    row  = conn.execute("SELECT user_id FROM papers WHERE rowid=?", (paper_id,)).fetchone()
    if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")
    conn.execute("UPDATE papers SET project_id=? WHERE rowid=?", (body.project_id, paper_id))
    conn.commit()
    conn.close()
    return {"paper_id": paper_id, "project_id": body.project_id}


# ─────────────────────────────────────────────
# Annotations  (reviewer_id = logged-in user's display_name)
# ─────────────────────────────────────────────
@app.get("/api/papers/{paper_id}/annotations")
def get_annotations(paper_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    reviewer_id = user["display_name"]
    conn  = get_db()
    rows  = conn.execute(
        "SELECT * FROM annotations WHERE paper_id=? AND reviewer_id=?",
        (paper_id, reviewer_id),
    ).fetchall()

    annotations = []
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except Exception:
            data = {}
        for col in ("correction_notes", "corrections_json", "pipeline_predictions_json", "field_annotations_json"):
            val = row[col] if col in row.keys() else None
            if val and col not in data:
                data[col] = val
        annotations.append({"id": row["id"], "reviewer_id": row["reviewer_id"], "timestamp": row["timestamp"], "data": data})

    spans = conn.execute(
        "SELECT * FROM spans WHERE paper_id=? AND reviewer_id=?",
        (paper_id, reviewer_id),
    ).fetchall()
    conn.close()
    return {"annotations": annotations, "spans": [dict(s) for s in spans]}


@app.post("/api/papers/{paper_id}/annotations")
def save_annotation(paper_id: int, payload: AnnotationPayload, ogai_session: str | None = Cookie(default=None)):
    user        = require_user(ogai_session)
    reviewer_id = user["display_name"]
    data        = payload.data.copy()

    correction_notes          = data.get("correction_notes", "") or ""
    corrections_json          = data.get("corrections_json", "") or ""
    pipeline_predictions_json = data.get("pipeline_predictions_json", "") or ""
    field_annotations_json    = json.dumps(payload.field_annotations) if payload.field_annotations else (data.get("field_annotations_json", "") or "")
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    with conn:
        conn.execute(
            """INSERT INTO annotations
                   (paper_id, reviewer_id, data_json, timestamp,
                    correction_notes, corrections_json, pipeline_predictions_json,
                    field_annotations_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id, reviewer_id) DO UPDATE SET
                   data_json=excluded.data_json, timestamp=excluded.timestamp,
                   correction_notes=excluded.correction_notes,
                   corrections_json=excluded.corrections_json,
                   pipeline_predictions_json=excluded.pipeline_predictions_json,
                   field_annotations_json=excluded.field_annotations_json""",
            (paper_id, reviewer_id, json.dumps(data), now,
             correction_notes, corrections_json, pipeline_predictions_json,
             field_annotations_json),
        )
        conn.execute("DELETE FROM spans WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id))
        for s in payload.spans:
            conn.execute(
                "INSERT INTO spans (paper_id, reviewer_id, field_name, page, text, x0, y0, x1, y1) VALUES (?,?,?,?,?,?,?,?,?)",
                (paper_id, reviewer_id, s.get("field_name"), s.get("page"), s.get("text"),
                 s.get("x0"), s.get("y0"), s.get("x1"), s.get("y1")),
            )
        conn.commit()
    conn.close()
    return {"status": "ok", "timestamp": now, "reviewer_id": reviewer_id}


# ─────────────────────────────────────────────
# AI prefill
# ─────────────────────────────────────────────
UNIVERSAL_IDS = [
    "citation_authors","citation_year","citation_title","citation_journal","citation_doi",
    "study_objective","population_participants","population_intervention_exposure",
    "population_comparator","population_outcomes","sample_size_total","sample_size_per_group",
    "power_calculation_reported","setting","country_region","study_period_enrollment_start",
    "study_period_enrollment_end","follow_up_duration","primary_outcome_definition",
    "primary_outcome_measurement","primary_outcome_timing","secondary_outcomes",
    "key_findings_effect_estimate","key_findings_metric","key_findings_ci_lower",
    "key_findings_ci_upper","key_findings_pvalue","key_findings_direction",
    "funding_source","conflicts_of_interest","limitations_stated","protocol_registration",
]

TYPE_FIELD_IDS: dict[str, list[str]] = {
    "Randomized Controlled Trial": ["randomization_method","allocation_concealment","allocation_ratio","stratification_factors","baseline_balance","blinding_participants","blinding_personnel","blinding_outcome_assessors","protocol_deviations","analysis_framework","attrition_rate","missing_data_handling","outcome_measurement_method","protocol_available","outcomes_match_protocol","consort_flow_diagram"],
    "Cluster Randomized Trial": ["cluster_unit","n_clusters","icc_reported","recruitment_after_randomization","clustering_in_analysis","contamination_risk"],
    "Stepped-Wedge Cluster RCT": ["cluster_unit","n_clusters","icc_reported","recruitment_after_randomization","clustering_in_analysis","contamination_risk"],
    "Crossover Trial": ["washout_period","carryover_assessment","period_effects","sequence_order","paired_analysis"],
    "Non-Randomized Trial": ["concurrent_control_confirmed","allocation_mechanism","baseline_comparability","confounding_control","blinding"],
    "Single-Arm Trial": ["primary_endpoint_prespecified","inclusion_exclusion_criteria","comparator_historical_reference","consecutive_enrolment"],
    "Dose-Escalation Study": ["escalation_scheme","dlt_definition","dose_levels","mtd_declared","rp2d","expansion_cohort","pk_pd_reported"],
    "Interrupted Time Series": ["n_data_points_pre","n_data_points_post","intervention_date","control_series","statistical_method","level_change","slope_change","autocorrelation_addressed","seasonality_adjustment","concurrent_events"],
    "Uncontrolled Before-After": ["pre_measurement","post_measurement","secular_trend_risk","regression_to_mean_risk","concurrent_events"],
    "Difference-in-Differences": ["exogenous_event","parallel_trends_evidence","n_pre_period_points","interaction_term","common_shocks","staggered_adoption"],
    "Regression Discontinuity": ["running_variable","cutoff_value","sharp_vs_fuzzy","bandwidth_selection","manipulation_testing","continuity_plots"],
    "Cohort Study": ["exposure_definition","exposure_measurement","comparator_group","outcome_ascertainment","confounders_measured","adjustment_method","loss_to_follow_up","immortal_time_bias"],
    "Case-Control": ["case_definition","case_source","control_selection","matching","exposure_ascertainment","recall_bias_risk"],
    "Case-Crossover": ["case_definition_ccx","exposure_definition_ccx","hazard_period","control_period","induction_period","temporal_direction","exposure_variability","conditional_logistic","self_selection_bias"],
    "Cross-Sectional (Analytical)": ["sampling_method","response_rate","exposure_outcome_simultaneity","adjustment_method"],
    "Mendelian Randomization": ["instrument_variants","f_statistic","mr_design","sample_overlap","pleiotropy_tests","exclusion_restriction"],
    "Diagnostic Accuracy": ["index_test","reference_standard","blinding_index_to_reference","blinding_reference_to_index","two_by_two_table","spectrum_of_patients","verification_bias","threshold_effects","flow_and_timing"],
    "Prognostic Factor Study": ["prognostic_factor","outcome_definition","study_participation","study_attrition","pf_measurement","confounding_control","statistical_analysis"],
    "Prediction Model Study": ["predictors_candidate","predictor_selection_method","model_type","discrimination","calibration","model_presentation","model_stage"],
    "SR without Meta-Analysis": ["search_strategy","inclusion_criteria","study_selection","data_extraction","included_studies_n","rob_tool_used","synthesis_method","grade_assessment","prisma_flow"],
    "SR with Meta-Analysis": ["search_strategy","inclusion_criteria","included_studies_n","effect_measure","pooled_estimate","pooling_model","heterogeneity","publication_bias","sensitivity_analyses","subgroup_analyses","grade_assessment","prisma_flow"],
    "Umbrella Review": ["search_strategy","inclusion_criteria","included_studies_n","rob_tool_used","synthesis_method","grade_assessment","prisma_flow"],
    "Network Meta-Analysis": ["search_strategy","inclusion_criteria","included_studies_n","effect_measure","pooled_estimate","heterogeneity","publication_bias","sensitivity_analyses","grade_assessment","prisma_flow"],
    "Economic Evaluation": ["evaluation_type","perspective","time_horizon","discount_rate","model_type","cost_inputs","effectiveness_source","icer","sensitivity_analysis"],
    "Guideline / Consensus": ["guideline_organization","panel_composition","evidence_base","grade_used","recommendations","updating_plan"],
    "Qualitative Research": ["methodology","data_collection","sampling_strategy","data_saturation","reflexivity","themes"],
}


def _build_prefill_prompt(study_type: str) -> str:
    all_ids    = UNIVERSAL_IDS + TYPE_FIELD_IDS.get(study_type, [])
    field_list = "\n".join(f"  - {f}" for f in all_ids)
    return f"""You are a clinical research data extractor. Extract information from this PDF to fill a structured annotation form for a {study_type} study.

Return ONLY a valid JSON object — no preamble, no markdown fences, no explanation. Keys must be exactly the field IDs below.

Rules:
- Short factual values (1–3 sentences max). Omit fields not found (do not include null or empty string).
- Do not invent values. Extract only what is explicitly stated.
- Numeric fields: return just the number/value as a string.
- DOI: return the DOI string only, without "https://doi.org/".

Fields:
{field_list}

Return only the JSON object."""


def _call_anthropic(pdf_bytes: bytes, prompt: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    model   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "anthropic-beta": "pdfs-2024-09-25",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error {e.code}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Network error: {e.reason}")

    text = next((b["text"].strip() for b in body.get("content", []) if b.get("type") == "text"), "")
    if not text:
        raise HTTPException(status_code=502, detail="Empty response from Anthropic API")

    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```")).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"JSON parse error: {e}. Got: {text[:300]}")


@app.post("/api/papers/{paper_id}/prefill")
async def prefill_fields(paper_id: int, body: PrefillRequest, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    row  = conn.execute("SELECT sha256, user_id FROM papers WHERE rowid=?", (paper_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    sha, uid = row["sha256"], row["user_id"]
    pdf_path = next(
        (p for p in [PAPERS_DIR / f"{sha[:16]}_{uid}.pdf", PAPERS_DIR / f"{sha[:16]}.pdf"] if p.exists()),
        None,
    )
    if not pdf_path:
        raise HTTPException(status_code=404, detail="PDF missing from disk")

    result = await asyncio.to_thread(_call_anthropic, pdf_path.read_bytes(), _build_prefill_prompt(body.study_type))
    return result


# ─────────────────────────────────────────────
# Admin: reset DB schema (adds missing columns)
# ─────────────────────────────────────────────
@app.api_route("/api/admin/reset-schema", methods=["GET","POST"])
def reset_schema(ogai_session: str | None = Cookie(default=None)):
    """Re-runs init_db() migrations. Safe to call on a live DB — only adds missing columns."""
    user = require_user(ogai_session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    init_db()
    # Also delete rows with no user_id and no usable file (orphans from pre-auth era)
    conn = get_db()
    deleted = conn.execute(
        "DELETE FROM papers WHERE user_id IS NULL"
    ).rowcount
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Schema migrations re-applied; {deleted} orphan rows deleted"}


# ─────────────────────────────────────────────
# Admin: DB debug info
# ─────────────────────────────────────────────
@app.get("/api/admin/debug-db")
def debug_db(ogai_session: str | None = Cookie(default=None)):
    """Return DB path, existence, paper rows, and PAPERS_DIR contents."""
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


# ─────────────────────────────────────────────
# CSV export  (user-scoped)
# ─────────────────────────────────────────────
FLAT_COLS = [
    "major_category","subcategory","study_type",
    "rule1_pass","rule2_pass","rule2b_pass","rule3_pass",
    "natural_experiment_flag","author_stated_design","author_label_discordance","reviewer_action",
    "citation_authors","citation_year","citation_title","citation_journal","citation_doi",
    "study_objective","population_participants","population_intervention_exposure",
    "population_comparator","population_outcomes","sample_size_total","sample_size_per_group",
    "power_calculation_reported","setting","country_region",
    "study_period_enrollment_start","study_period_enrollment_end","follow_up_duration",
    "primary_outcome_definition","primary_outcome_measurement","primary_outcome_timing",
    "secondary_outcomes","key_findings_effect_estimate","key_findings_metric",
    "key_findings_ci_lower","key_findings_ci_upper","key_findings_pvalue","key_findings_direction",
    "funding_source","conflicts_of_interest","limitations_stated","protocol_registration",
    "clinical_trial_phase","regulatory_context","registration_number","industry_sponsored",
    "data_source_type","database_name","adaptive_design","pragmatic_vs_explanatory",
    "trial_framework","target_trial_emulation","pilot_or_feasibility",
    "correction_notes","corrections_json","pipeline_predictions_json","field_annotations_json",
]

# Analytics-friendly field annotation columns appended per-field
# Each field tracked in fieldAnnotations gets: <field>__ann_status, <field>__ai_value,
# <field>__human_value, <field>__flag, <field>__flag_note


def _csv_row(vals: list) -> str:
    def _e(v):
        v = str(v).replace('"', '""')
        return f'"{v}"' if any(c in v for c in (',', '"', '\n', '\r')) else v
    return ",".join(_e(v) for v in vals) + "\r\n"


def _build_export_rows(
    user_id: int,
    paper_id: Optional[int] = None,
    project_id: Optional[int] = None,
) -> tuple[list[str], str]:
    """
    Returns (rows, filename) where rows is a list of CSV line strings (incl. header).
    Filters: paper_id → single paper; project_id → all papers in that project;
    neither → all user papers.
    """
    conn = get_db()
    papers_q = "SELECT id, filename, project_id FROM papers WHERE user_id=?"
    params: list = [user_id]
    if paper_id is not None:
        papers_q += " AND id=?"
        params.append(paper_id)
    elif project_id is not None:
        papers_q += " AND project_id=?"
        params.append(project_id)

    papers = {p["id"]: dict(p) for p in conn.execute(papers_q, params).fetchall()}
    proj_names = {p["id"]: p["name"] for p in conn.execute(
        "SELECT id, name FROM projects WHERE user_id=?", (user_id,)
    ).fetchall()}

    paper_ids = list(papers.keys())
    annotations = []
    if paper_ids:
        ph = ",".join("?" * len(paper_ids))
        annotations = conn.execute(
            f"SELECT * FROM annotations WHERE paper_id IN ({ph})", paper_ids
        ).fetchall()
    conn.close()

    # Collect all field_ids mentioned in any field_annotations_json so we
    # can build consistent extra columns across the export
    all_annotated_fields: set[str] = set()
    parsed_anns: list[dict] = []
    for ann in annotations:
        try:
            fa = json.loads(
                (ann["field_annotations_json"] if "field_annotations_json" in ann.keys() else None) or "{}"
            )
        except Exception:
            fa = {}
        all_annotated_fields.update(fa.keys())
        parsed_anns.append(fa)

    sorted_fields = sorted(all_annotated_fields)
    ann_extra_cols = []
    for fid in sorted_fields:
        ann_extra_cols += [
            f"{fid}__ann_status",
            f"{fid}__ai_value",
            f"{fid}__corrected_value",
            f"{fid}__flagged",
            f"{fid}__flag_note",
        ]

    header = ["filename", "project", "reviewer_id", "timestamp"] + FLAT_COLS + ann_extra_cols
    rows: list[str] = [_csv_row(header)]

    for ann, fa in zip(annotations, parsed_anns):
        try:
            data = json.loads(ann["data_json"] or "{}")
        except Exception:
            data = {}
        for col in ("correction_notes", "corrections_json", "pipeline_predictions_json", "field_annotations_json"):
            val = ann[col] if col in ann.keys() else None
            if val:
                data[col] = val

        p = papers.get(ann["paper_id"], {})
        proj_name = proj_names.get(p.get("project_id"), "") if p.get("project_id") else ""
        base = [p.get("filename", ""), proj_name, ann["reviewer_id"], ann["timestamp"]]
        base += [data.get(c, "") for c in FLAT_COLS]

        # Append per-field annotation analytics columns
        for fid in sorted_fields:
            fann = fa.get(fid, {})
            base += [
                fann.get("status", ""),
                fann.get("ai_value", ""),
                fann.get("corrected_value", ""),
                "Yes" if fann.get("flagged") else "",
                fann.get("flag_note", ""),
            ]

        rows.append(_csv_row(base))

    # Choose a sensible filename
    if paper_id is not None and paper_ids:
        fn = f"ogai_{papers[paper_ids[0]]['filename'].replace('.pdf','')}.csv"
    elif project_id is not None:
        fn = f"ogai_project_{proj_names.get(project_id, str(project_id))}.csv".replace(" ", "_")
    else:
        fn = "ogai_annotations.csv"

    return rows, fn


@app.get("/api/export/csv")
def export_csv(
    paper_id:   Optional[int] = Query(default=None),
    project_id: Optional[int] = Query(default=None),
    ogai_session: str | None = Cookie(default=None),
):
    user = require_user(ogai_session)
    rows, filename = _build_export_rows(user["id"], paper_id=paper_id, project_id=project_id)
    safe_fn = filename.replace('"', '')
    return StreamingResponse(
        iter(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_fn}"'},
    )
