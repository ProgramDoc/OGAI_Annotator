"""
OGAI Annotation Platform — Backend
====================================
FastAPI backend serving:
  - PDF upload and storage
  - Annotation persistence (SQLite)
  - Multi-reviewer support
  - CSV export
  - Static frontend (index.html)

Run:
    uvicorn main:app --reload --port 8000

Or with multiple workers (shared server):
    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
"""

import json
import csv
import io
import os
import shutil
import sqlite3
import hashlib
import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
FRONTEND   = BASE_DIR / "frontend"

DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", str(BASE_DIR)))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
PAPERS_DIR.mkdir(exist_ok=True)

# ── FastAPI app ───────────────────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database init ─────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            upload_time TEXT NOT NULL,
            file_path   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS annotations (
            paper_id    TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            data_json   TEXT NOT NULL,
            PRIMARY KEY (paper_id, reviewer_id),
            FOREIGN KEY (paper_id) REFERENCES papers(id)
        );

        CREATE TABLE IF NOT EXISTS spans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id    TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            field_name  TEXT NOT NULL,
            page        INTEGER NOT NULL,
            text        TEXT NOT NULL,
            x0          REAL, y0 REAL, x1 REAL, y1 REAL,
            timestamp   TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Pydantic models ───────────────────────────────────────────────────────
class AnnotationSave(BaseModel):
    reviewer_id: str
    data: dict          # all form field values
    spans: list[dict]   # [{field_name, page, text, x0, y0, x1, y1}]

class SpanDelete(BaseModel):
    reviewer_id: str
    field_name: str
    page: int
    text: str

# ── Paper endpoints ───────────────────────────────────────────────────────

@app.get("/api/papers")
def list_papers():
    conn = get_db()
    papers = conn.execute("SELECT id, filename, upload_time FROM papers ORDER BY upload_time DESC").fetchall()
    result = []
    for p in papers:
        reviewers = conn.execute(
            "SELECT DISTINCT reviewer_id FROM annotations WHERE paper_id=?", (p["id"],)
        ).fetchall()
        result.append({
            "id": p["id"],
            "filename": p["filename"],
            "upload_time": p["upload_time"],
            "annotated_by": [r["reviewer_id"] for r in reviewers],
        })
    conn.close()
    return result


@app.post("/api/papers/upload")
async def upload_paper(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    content = await file.read()
    # Use SHA-256 of content as stable ID (deduplication)
    paper_id = hashlib.sha256(content).hexdigest()[:16]

    dest = PAPERS_DIR / f"{paper_id}.pdf"
    if not dest.exists():
        dest.write_bytes(content)

    conn = get_db()
    existing = conn.execute("SELECT id FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO papers VALUES (?,?,?,?)",
            (paper_id, file.filename, datetime.datetime.utcnow().isoformat(), str(dest)),
        )
        conn.commit()
    conn.close()

    return {"paper_id": paper_id, "filename": file.filename}


@app.get("/api/papers/{paper_id}/pdf")
def serve_pdf(paper_id: str):
    conn = get_db()
    row = conn.execute("SELECT file_path, filename FROM papers WHERE id=?", (paper_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Paper not found")
    return FileResponse(row["file_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{row["filename"]}"'})


@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: str):
    conn = get_db()
    row = conn.execute("SELECT file_path FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Paper not found")
    conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    conn.execute("DELETE FROM annotations WHERE paper_id=?", (paper_id,))
    conn.execute("DELETE FROM spans WHERE paper_id=?", (paper_id,))
    conn.commit()
    conn.close()
    p = Path(row["file_path"])
    if p.exists():
        p.unlink()
    return {"deleted": paper_id}


# ── Annotation endpoints ──────────────────────────────────────────────────

@app.get("/api/papers/{paper_id}/annotations")
def get_annotations(paper_id: str, reviewer_id: Optional[str] = None):
    conn = get_db()
    # Return all reviewers' annotations for this paper (for IRR view)
    if reviewer_id:
        rows = conn.execute(
            "SELECT reviewer_id, timestamp, data_json FROM annotations WHERE paper_id=? AND reviewer_id=?",
            (paper_id, reviewer_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT reviewer_id, timestamp, data_json FROM annotations WHERE paper_id=?",
            (paper_id,)
        ).fetchall()

    span_rows = conn.execute(
        "SELECT field_name, page, text, x0, y0, x1, y1, reviewer_id FROM spans WHERE paper_id=?"
        + (" AND reviewer_id=?" if reviewer_id else ""),
        (paper_id, reviewer_id) if reviewer_id else (paper_id,)
    ).fetchall()
    conn.close()

    return {
        "annotations": [
            {"reviewer_id": r["reviewer_id"], "timestamp": r["timestamp"],
             "data": json.loads(r["data_json"])}
            for r in rows
        ],
        "spans": [dict(s) for s in span_rows],
    }


@app.post("/api/papers/{paper_id}/annotations")
def save_annotation(paper_id: str, body: AnnotationSave):
    conn = get_db()
    paper = conn.execute("SELECT id FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not paper:
        conn.close()
        raise HTTPException(404, "Paper not found")

    now = datetime.datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO annotations (paper_id, reviewer_id, timestamp, data_json)
        VALUES (?,?,?,?)
        ON CONFLICT(paper_id, reviewer_id) DO UPDATE SET
            timestamp=excluded.timestamp, data_json=excluded.data_json
    """, (paper_id, body.reviewer_id, now, json.dumps(body.data)))

    # Replace spans for this reviewer/paper
    conn.execute("DELETE FROM spans WHERE paper_id=? AND reviewer_id=?",
                 (paper_id, body.reviewer_id))
    for s in body.spans:
        conn.execute("""
            INSERT INTO spans (paper_id, reviewer_id, field_name, page, text, x0, y0, x1, y1, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (paper_id, body.reviewer_id, s.get("field_name"), s.get("page", 0),
              s.get("text", ""), s.get("x0"), s.get("y0"), s.get("x1"), s.get("y1"), now))

    conn.commit()
    conn.close()
    return {"saved": True, "timestamp": now}


# ── Export endpoint ───────────────────────────────────────────────────────

FLAT_COLS = [
    "paper_id", "filename", "reviewer_id", "timestamp",
    # Layer 1 universal
    "citation_authors", "citation_year", "citation_title", "citation_journal", "citation_doi",
    "study_objective",
    "population_participants", "population_intervention_exposure",
    "population_comparator", "population_outcomes",
    "sample_size_total", "sample_size_per_group", "power_calculation_reported",
    "setting", "country_region",
    "study_period_enrollment_start", "study_period_enrollment_end", "follow_up_duration",
    "primary_outcome_definition", "primary_outcome_measurement", "primary_outcome_timing",
    "secondary_outcomes",
    "key_findings_effect_estimate", "key_findings_metric",
    "key_findings_ci_lower", "key_findings_ci_upper", "key_findings_pvalue", "key_findings_direction",
    "funding_source", "conflicts_of_interest",
    "author_stated_design", "limitations_stated", "protocol_registration",
    # Stage 1 classification
    "major_category", "subcategory", "study_type",
    "rule1_pass", "rule2_pass", "rule2b_pass", "rule3_pass",
    "reviewer_action", "author_label_discordance",
    "natural_experiment_flag",
    # Layer 2 type-specific
    "type_specific_fields_json",
    # Layer 3 modifiers
    "clinical_trial_phase", "regulatory_context", "registration_number",
    "industry_sponsored", "data_source_type", "database_name",
    "adaptive_design", "pragmatic_vs_explanatory", "trial_framework",
    "target_trial_emulation", "pilot_or_feasibility",
]

@app.get("/api/export/csv")
def export_csv():
    conn = get_db()
    papers = {r["id"]: r["filename"]
              for r in conn.execute("SELECT id, filename FROM papers").fetchall()}
    ann_rows = conn.execute(
        "SELECT paper_id, reviewer_id, timestamp, data_json FROM annotations"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FLAT_COLS, extrasaction="ignore")
    writer.writeheader()

    for row in ann_rows:
        data = json.loads(row["data_json"])
        flat = {c: data.get(c, "") for c in FLAT_COLS}
        flat["paper_id"]   = row["paper_id"]
        flat["filename"]   = papers.get(row["paper_id"], "")
        flat["reviewer_id"] = row["reviewer_id"]
        flat["timestamp"]  = row["timestamp"]
        # Serialize type_specific back to JSON string for CSV
        if isinstance(flat.get("type_specific_fields_json"), dict):
            flat["type_specific_fields_json"] = json.dumps(flat["type_specific_fields_json"])
        writer.writerow(flat)

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ogai_annotations.csv"},
    )


# ── Serve frontend ────────────────────────────────────────────────────────
# Mount frontend last so API routes take priority
app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
