"""
Paper management routes: upload, list, serve PDF, delete, assign to project.
"""

import hashlib
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .auth import require_user
from .config import PAPERS_DIR, DB_PATH
from .db import get_db

logger = logging.getLogger("ogai")
router = APIRouter(prefix="/api/papers", tags=["papers"])

# Maximum upload size: 50 MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# PDF magic bytes
PDF_MAGIC = b"%PDF-"


class PaperAssign(BaseModel):
    project_id: Optional[int] = None


def _papers_columns() -> set[str]:
    conn = get_db()
    try:
        rows = conn.execute("PRAGMA table_info(papers)").fetchall()
        return {row["name"] for row in rows}
    except Exception:
        return set()
    finally:
        conn.close()


@router.get("")
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


@router.post("/upload", status_code=201)
async def upload_paper(file: UploadFile = File(...), ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    data = await file.read()

    # ── Validate file size ──
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")

    # ── Validate PDF magic bytes ──
    if not data[:5].startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="Invalid file: not a PDF")

    sha  = hashlib.sha256(data).hexdigest()
    uid  = user["id"]

    # ── Write PDF to disk ──
    dest = PAPERS_DIR / f"{sha[:16]}_{uid}.pdf"
    if not dest.exists():
        legacy = PAPERS_DIR / f"{sha[:16]}.pdf"
        dest.write_bytes(legacy.read_bytes() if legacy.exists() else data)
    disk_fn = dest.name

    # ── Discover DB columns + NOT NULL constraints ──
    pragma_conn = get_db()
    pragma_rows = pragma_conn.execute("PRAGMA table_info(papers)").fetchall()
    pragma_conn.close()
    col_info = {r["name"]: {"notnull": r["notnull"], "dflt": r["dflt_value"]} for r in pragma_rows}
    cols = set(col_info.keys())

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    LEGACY_DEFAULTS = {
        "file_path":   disk_fn,
        "upload_time": now_str,
        "created_at":  now_str,
        "timestamp":   now_str,
    }

    conn = get_db()
    try:
        insert_cols = ["filename"]
        insert_vals = [file.filename]
        if "sha256"        in cols: insert_cols.append("sha256");        insert_vals.append(sha)
        if "user_id"       in cols: insert_cols.append("user_id");       insert_vals.append(uid)
        if "disk_filename" in cols: insert_cols.append("disk_filename"); insert_vals.append(disk_fn)

        for col, info in col_info.items():
            if col in insert_cols or col == "id":
                continue
            if info["notnull"] and info["dflt"] is None:
                fallback = LEGACY_DEFAULTS.get(col, "")
                insert_cols.append(col)
                insert_vals.append(fallback)

        placeholders = ",".join("?" * len(insert_cols))
        insert_sql   = f"INSERT OR IGNORE INTO papers ({','.join(insert_cols)}) VALUES ({placeholders})"

        cur = conn.execute(insert_sql, insert_vals)
        conn.commit()
        rowid = cur.lastrowid

        # Backfill id=rowid (legacy TEXT PRIMARY KEY schema)
        try:
            conn.execute(
                "UPDATE papers SET id=CAST(rowid AS TEXT) WHERE rowid=? AND (id IS NULL OR id='')",
                (rowid,)
            )
        except Exception:
            pass
        if "user_id" in cols:
            conn.execute("UPDATE papers SET user_id=? WHERE rowid=?", (uid, rowid))
        if "disk_filename" in cols:
            conn.execute("UPDATE papers SET disk_filename=? WHERE rowid=?", (disk_fn, rowid))
        conn.execute("UPDATE papers SET filename=? WHERE rowid=?", (file.filename, rowid))
        conn.commit()

        sel_cols = ["filename"]
        if "project_id" in cols: sel_cols.append("project_id")
        row = conn.execute(
            f"SELECT {','.join(sel_cols)}, rowid FROM papers WHERE rowid=?", (rowid,)
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=500, detail=f"Upload failed: rowid={rowid} not found after insert")

        proj_id = row["project_id"] if "project_id" in cols else None
        return {"id": rowid, "filename": file.filename, "project_id": proj_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("upload error: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Upload error: {str(e)}")
    finally:
        conn.close()


@router.get("/{paper_id}/pdf")
def get_pdf(paper_id: int, ogai_session: str | None = Cookie(default=None)):
    user = require_user(ogai_session)
    conn = get_db()

    cols = _papers_columns()
    sel  = ["sha256", "user_id", "disk_filename"]
    if "file_path" in cols: sel.append("file_path")

    row  = conn.execute(
        f"SELECT {','.join(sel)} FROM papers WHERE id=?", (paper_id,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    sha      = row["sha256"] or ""
    uid      = row["user_id"] or 0
    disk_fn  = row["disk_filename"] or ""
    file_path = row["file_path"] if "file_path" in cols else ""

    candidates = []
    if disk_fn:
        candidates.append(PAPERS_DIR / disk_fn)
    if file_path:
        candidates.append(PAPERS_DIR / Path(file_path).name)
        candidates.append(Path(file_path))
    if sha:
        candidates += [
            PAPERS_DIR / f"{sha[:16]}_{uid}.pdf",
            PAPERS_DIR / f"{sha[:16]}.pdf",
        ]

    for c in candidates:
        if c.exists():
            return FileResponse(str(c), media_type="application/pdf")

    raise HTTPException(status_code=404, detail="PDF file missing from disk")


@router.delete("/{paper_id}", status_code=204)
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


@router.post("/{paper_id}/assign")
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
