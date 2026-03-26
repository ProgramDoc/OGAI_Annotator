import os
import csv
import sqlite3
import io
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent   # → repo root
FRONTEND = BASE_DIR / "frontend"

# On Render, use the persistent disk at /data
# Locally, use the repo root (same behaviour as before)
DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", str(BASE_DIR)))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
PAPERS_DIR.mkdir(exist_ok=True)

# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform")


# ── Database ─────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer_id  TEXT    NOT NULL,
            paper_id     TEXT    NOT NULL,
            study_type   TEXT,
            population   TEXT,
            intervention TEXT,
            comparator   TEXT,
            outcome      TEXT,
            notes        TEXT,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ── Pydantic models ───────────────────────────────────────────────────────
class Annotation(BaseModel):
    reviewer_id:  str
    paper_id:     str
    study_type:   Optional[str] = None
    population:   Optional[str] = None
    intervention: Optional[str] = None
    comparator:   Optional[str] = None
    outcome:      Optional[str] = None
    notes:        Optional[str] = None


class AnnotationUpdate(BaseModel):
    study_type:   Optional[str] = None
    population:   Optional[str] = None
    intervention: Optional[str] = None
    comparator:   Optional[str] = None
    outcome:      Optional[str] = None
    notes:        Optional[str] = None


# ── Frontend ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = FRONTEND / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=500, detail="frontend/index.html not found")
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


# ── Papers API ────────────────────────────────────────────────────────────
@app.get("/api/papers")
async def list_papers():
    papers = sorted(p.name for p in PAPERS_DIR.iterdir() if p.suffix.lower() == ".pdf")
    return {"papers": papers}


@app.post("/api/papers/upload")
async def upload_paper(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    dest = PAPERS_DIR / file.filename
    content = await file.read()
    dest.write_bytes(content)
    return {"filename": file.filename, "size": len(content)}


@app.get("/api/papers/{filename}")
async def serve_paper(filename: str):
    paper_path = PAPERS_DIR / filename
    if not paper_path.exists():
        raise HTTPException(status_code=404, detail="Paper not found")
    # Prevent path traversal
    if not paper_path.resolve().is_relative_to(PAPERS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return FileResponse(str(paper_path), media_type="application/pdf")


# ── Annotations API ───────────────────────────────────────────────────────
@app.get("/api/annotations")
async def list_annotations(reviewer_id: Optional[str] = None, paper_id: Optional[str] = None):
    conn = get_db()
    query = "SELECT * FROM annotations WHERE 1=1"
    params: list = []
    if reviewer_id:
        query += " AND reviewer_id = ?"
        params.append(reviewer_id)
    if paper_id:
        query += " AND paper_id = ?"
        params.append(paper_id)
    query += " ORDER BY updated_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"annotations": [dict(r) for r in rows]}


@app.post("/api/annotations", status_code=201)
async def create_annotation(annotation: Annotation):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO annotations
           (reviewer_id, paper_id, study_type, population, intervention, comparator, outcome, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            annotation.reviewer_id,
            annotation.paper_id,
            annotation.study_type,
            annotation.population,
            annotation.intervention,
            annotation.comparator,
            annotation.outcome,
            annotation.notes,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    row = conn.execute("SELECT * FROM annotations WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return dict(row)


@app.put("/api/annotations/{annotation_id}")
async def update_annotation(annotation_id: int, update: AnnotationUpdate):
    conn = get_db()
    row = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Annotation not found")
    conn.execute(
        """UPDATE annotations SET
           study_type=?, population=?, intervention=?, comparator=?, outcome=?, notes=?,
           updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            update.study_type if update.study_type is not None else row["study_type"],
            update.population if update.population is not None else row["population"],
            update.intervention if update.intervention is not None else row["intervention"],
            update.comparator if update.comparator is not None else row["comparator"],
            update.outcome if update.outcome is not None else row["outcome"],
            update.notes if update.notes is not None else row["notes"],
            annotation_id,
        ),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.delete("/api/annotations/{annotation_id}", status_code=204)
async def delete_annotation(annotation_id: int):
    conn = get_db()
    row = conn.execute("SELECT id FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Annotation not found")
    conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    conn.commit()
    conn.close()


# ── Export CSV ────────────────────────────────────────────────────────────
@app.get("/api/annotations/export")
async def export_annotations():
    conn = get_db()
    rows = conn.execute("SELECT * FROM annotations ORDER BY reviewer_id, paper_id, updated_at").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    if rows:
        writer.writerow(rows[0].keys())
        writer.writerows([list(r) for r in rows])
    else:
        writer.writerow(["id", "reviewer_id", "paper_id", "study_type", "population",
                         "intervention", "comparator", "outcome", "notes", "created_at", "updated_at"])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=annotations.csv"},
    )
