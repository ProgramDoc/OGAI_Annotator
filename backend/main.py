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
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
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
        "SELECT id, filename, project_id FROM papers WHERE user_id=? ORDER BY id DESC",
        (user["id"],),
    ).fetchall()
    result = []
    for p in papers:
        reviewers = conn.execute(
            "SELECT reviewer_id FROM annotations WHERE paper_id=?", (p["id"],)
        ).fetchall()
        result.append({
            "id":           p["id"],
            "filename":     p["filename"],
            "project_id":   p["project_id"],
            "annotated_by": [r["reviewer_id"] for r in reviewers],
        })
    conn.close()
    return result


@app.post("/api/papers/upload", status_code=201)
async def upload_paper(file: UploadFile = File(...), ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    data = await file.read()
    sha  = hashlib.sha256(data).hexdigest()
    uid  = user["id"]

    # ── Step 1: write PDF bytes to disk ─────────────────────────────────────
    dest = PAPERS_DIR / f"{sha[:16]}_{uid}.pdf"
    if not dest.exists():
        legacy = PAPERS_DIR / f"{sha[:16]}.pdf"
        dest.write_bytes(legacy.read_bytes() if legacy.exists() else data)
    disk_fn = dest.name

    conn = get_db()
    try:
        # ── Step 2: ensure a row exists (INSERT OR IGNORE on minimal columns) ──
        # We try columns one at a time, ignoring OperationalError (column missing)
        # and IntegrityError (row already exists — fine, we SELECT after).
        for insert_sql, insert_params in [
            ("INSERT OR IGNORE INTO papers (filename, sha256, user_id, disk_filename) VALUES (?,?,?,?)",
             (file.filename, sha, uid, disk_fn)),
            ("INSERT OR IGNORE INTO papers (filename, sha256, user_id) VALUES (?,?,?)",
             (file.filename, sha, uid)),
            ("INSERT OR IGNORE INTO papers (filename, sha256) VALUES (?,?)",
             (file.filename, sha)),
            ("INSERT OR IGNORE INTO papers (filename) VALUES (?)",
             (file.filename,)),
        ]:
            try:
                conn.execute(insert_sql, insert_params)
                conn.commit()
                logger.info(f"upload: INSERT succeeded with sql={insert_sql[:60]}")
                break
            except sqlite3.OperationalError as e:
                logger.warning(f"upload: INSERT column missing ({e}), trying simpler schema")
                continue
            except Exception as e:
                logger.error(f"upload: INSERT unexpected error: {e}")
                break

        # ── Step 3: claim ownership via UPDATE (each column separate, fault-tolerant) ──
        for upd_sql, upd_params in [
            ("UPDATE papers SET user_id=? WHERE sha256=? AND user_id IS NULL", (uid, sha)),
            ("UPDATE papers SET disk_filename=? WHERE sha256=? AND disk_filename IS NULL", (disk_fn, sha)),
            ("UPDATE papers SET filename=? WHERE sha256=?", (file.filename, sha)),
        ]:
            try:
                conn.execute(upd_sql, upd_params)
                conn.commit()
            except sqlite3.OperationalError as e:
                logger.warning(f"upload: UPDATE column missing ({e}), skipping")
            except Exception as e:
                logger.warning(f"upload: UPDATE failed ({e}), continuing")

        # ── Step 4: fetch the row ────────────────────────────────────────────
        # Try increasingly permissive SELECTs
        paper_id   = None
        project_id = None
        filename   = file.filename

        for sel_sql, sel_params in [
            ("SELECT id, filename, project_id FROM papers WHERE sha256=? AND user_id=?", (sha, uid)),
            ("SELECT id, filename, project_id FROM papers WHERE sha256=?", (sha,)),
            ("SELECT id, filename FROM papers WHERE sha256=?", (sha,)),
        ]:
            try:
                row = conn.execute(sel_sql, sel_params).fetchone()
                if row:
                    paper_id = row["id"]
                    filename = row["filename"]
                    project_id = row["project_id"] if "project_id" in row.keys() else None
                    break
            except sqlite3.OperationalError as e:
                logger.warning(f"upload: SELECT failed ({e}), trying simpler query")
                continue

        if paper_id is None:
            # Last resort: get highest id row with this filename
            try:
                row = conn.execute(
                    "SELECT id, filename FROM papers WHERE filename=? ORDER BY id DESC LIMIT 1",
                    (file.filename,)
                ).fetchone()
                if row:
                    paper_id = row["id"]
                    filename = row["filename"]
                    logger.warning(f"upload: fell back to filename-based SELECT, id={paper_id}")
            except Exception as e:
                logger.error(f"upload: last-resort SELECT failed: {e}")

        if paper_id is None:
            tb = traceback.format_exc()
            logger.error(f"upload: all strategies exhausted. sha={sha[:8]} uid={uid}\n{tb}")
            raise HTTPException(status_code=500, detail=f"Upload failed — all insert strategies exhausted (sha={sha[:8]})")

        logger.info(f"upload: success paper_id={paper_id} sha={sha[:8]}")
        return {"id": paper_id, "filename": filename, "project_id": project_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"upload: unhandled exception: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Upload error: {str(e)}")
    finally:
        conn.close()


@app.get("/api/papers/{paper_id}/pdf")
def get_pdf(paper_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    row  = conn.execute("SELECT sha256, user_id, disk_filename FROM papers WHERE id=?", (paper_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    candidates = []
    if row["disk_filename"]:
        candidates.append(PAPERS_DIR / row["disk_filename"])
    sha = row["sha256"] or ""
    uid = row["user_id"] or 0
    if sha:
        candidates += [PAPERS_DIR / f"{sha[:16]}_{uid}.pdf", PAPERS_DIR / f"{sha[:16]}.pdf"]
    for c in candidates:
        if c.exists():
            return FileResponse(str(c), media_type="application/pdf")
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
    conn.execute("DELETE FROM papers      WHERE id=?",       (paper_id,))
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
    row  = conn.execute("SELECT user_id FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")
    conn.execute("UPDATE papers SET project_id=? WHERE id=?", (body.project_id, paper_id))
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
        for col in ("correction_notes", "corrections_json", "pipeline_predictions_json"):
            if row[col] and col not in data:
                data[col] = row[col]
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
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    with conn:
        conn.execute(
            """INSERT INTO annotations
                   (paper_id, reviewer_id, data_json, timestamp,
                    correction_notes, corrections_json, pipeline_predictions_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(paper_id, reviewer_id) DO UPDATE SET
                   data_json=excluded.data_json, timestamp=excluded.timestamp,
                   correction_notes=excluded.correction_notes,
                   corrections_json=excluded.corrections_json,
                   pipeline_predictions_json=excluded.pipeline_predictions_json""",
            (paper_id, reviewer_id, json.dumps(data), now,
             correction_notes, corrections_json, pipeline_predictions_json),
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
    row  = conn.execute("SELECT sha256, user_id FROM papers WHERE id=?", (paper_id,)).fetchone()
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
@app.post("/api/admin/reset-schema")
def reset_schema(ogai_session: str | None = Cookie(default=None)):
    """Re-runs init_db() migrations. Safe to call on a live DB — only adds missing columns."""
    user = require_user(ogai_session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    init_db()
    return {"status": "ok", "message": "Schema migrations re-applied"}


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
    "correction_notes","corrections_json","pipeline_predictions_json",
]


def _csv_row(vals: list) -> str:
    def _e(v):
        v = str(v).replace('"', '""')
        return f'"{v}"' if any(c in v for c in (',', '"', '\n', '\r')) else v
    return ",".join(_e(v) for v in vals) + "\r\n"


@app.get("/api/export/csv")
def export_csv(ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()
    papers = {p["id"]: dict(p) for p in conn.execute(
        "SELECT id, filename, project_id FROM papers WHERE user_id=?", (user["id"],)
    ).fetchall()}
    proj_names = {p["id"]: p["name"] for p in conn.execute(
        "SELECT id, name FROM projects WHERE user_id=?", (user["id"],)
    ).fetchall()}
    paper_ids = list(papers.keys())
    annotations = []
    if paper_ids:
        ph = ",".join("?" * len(paper_ids))
        annotations = conn.execute(f"SELECT * FROM annotations WHERE paper_id IN ({ph})", paper_ids).fetchall()
    conn.close()

    rows = [_csv_row(["filename","project","reviewer_id","timestamp"] + FLAT_COLS)]
    for ann in annotations:
        try:
            data = json.loads(ann["data_json"] or "{}")
        except Exception:
            data = {}
        for col in ("correction_notes","corrections_json","pipeline_predictions_json"):
            if ann[col]:
                data[col] = ann[col]
        p = papers.get(ann["paper_id"], {})
        proj_name = proj_names.get(p.get("project_id"), "") if p.get("project_id") else ""
        rows.append(_csv_row([p.get("filename",""), proj_name, ann["reviewer_id"], ann["timestamp"]] + [data.get(c,"") for c in FLAT_COLS]))

    return StreamingResponse(iter(rows), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ogai_annotations.csv"})
