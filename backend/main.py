"""
OGAI Annotation Platform — Backend v1.1
========================================
FastAPI backend serving:
  - PDF upload and storage
  - Annotation persistence (SQLite)
  - Multi-reviewer support
  - CSV export
  - AI-assisted field extraction (Anthropic API)
  - Static frontend (index.html)

Run:
    uvicorn main:app --reload --port 8000

Environment variables:
    ANTHROPIC_API_KEY   — required for AI auto-fill feature
    RENDER_DATA_DIR     — set to /data on Render.com; defaults to repo root locally
"""

import json
import csv
import io
import os
import re
import base64
import shutil
import sqlite3
import hashlib
import asyncio
import datetime
import urllib.request
import urllib.error
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

# Use /data (Render persistent disk) in production, repo root locally
DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", str(BASE_DIR)))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
PAPERS_DIR.mkdir(exist_ok=True)

# ── FastAPI app ───────────────────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform", version="1.1")

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
    data: dict
    spans: list[dict]

class SpanDelete(BaseModel):
    reviewer_id: str
    field_name: str
    page: int
    text: str

class PrefillRequest(BaseModel):
    study_type: str
    fields: list[dict]   # [{id, label}] from frontend

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


# ── AI Auto-fill endpoint ─────────────────────────────────────────────────

def _call_anthropic(api_key: str, pdf_b64: str, study_type: str, fields: list) -> dict:
    """
    Synchronous Anthropic API call — runs in a thread pool via asyncio.to_thread.
    Sends the PDF as a base64 document and a structured extraction prompt.
    Returns dict of {field_id: extracted_value_string}.
    """
    field_lines = "\n".join(
        f'  "{f["id"]}": {f.get("label", f["id"])}'
        for f in fields
    )
    prompt = (
        f"You are a systematic review data extraction assistant.\n"
        f"Study type: {study_type}\n\n"
        f"Extract the following fields from the attached research paper. "
        f"Return ONLY a valid JSON object — no markdown fences, no preamble, no explanation. "
        f"Use the exact key names shown. "
        f"Values must be concise strings (not nested objects). "
        f"If a field is not reported in the paper, use an empty string \"\".\n\n"
        f"Fields to extract:\n{{\n{field_lines}\n}}"
    )

    payload = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Anthropic API error {e.code}: {err_body[:400]}")

    content_blocks = body.get("content", [])
    text = next((b["text"] for b in content_blocks if b.get("type") == "text"), "")
    text = text.strip()

    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Model returned non-JSON: {text[:400]}")


@app.post("/api/papers/{paper_id}/prefill")
async def prefill_fields(paper_id: str, body: PrefillRequest):
    """
    Extract extraction fields from a PDF using the Anthropic API.
    Set ANTHROPIC_API_KEY environment variable to enable.
    Returns dict of {field_id: extracted_value_string}.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY is not configured. Add it as an environment variable in the Render dashboard."
        )

    conn = get_db()
    row = conn.execute("SELECT file_path FROM papers WHERE id=?", (paper_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Paper not found")

    pdf_path = Path(row["file_path"])
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file not found on disk")

    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()

    try:
        result = await asyncio.to_thread(
            _call_anthropic, api_key, pdf_b64, body.study_type, body.fields
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    return result


# ── Export endpoint ───────────────────────────────────────────────────────

FLAT_COLS = [
    "paper_id", "filename", "reviewer_id", "timestamp",
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
    "major_category", "subcategory", "study_type",
    "rule1_pass", "rule2_pass", "rule2b_pass", "rule3_pass",
    "reviewer_action", "author_label_discordance",
    "natural_experiment_flag",
    "type_specific_fields_json",
    "clinical_trial_phase", "regulatory_context", "registration_number",
    "industry_sponsored", "data_source_type", "database_name",
    "adaptive_design", "pragmatic_vs_explanatory", "trial_framework",
    "target_trial_emulation", "pilot_or_feasibility",
    # Annotation quality & corrections
    "correction_notes",
    "corrections_json",          # structured diff {field: {from, to}} for Correct/Flag actions
    "pipeline_predictions_json", # baseline AI/pipeline values at time of fill
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
        flat["paper_id"]    = row["paper_id"]
        flat["filename"]    = papers.get(row["paper_id"], "")
        flat["reviewer_id"] = row["reviewer_id"]
        flat["timestamp"]   = row["timestamp"]
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

@app.get("/", include_in_schema=False)
def root():
    """Serve annotator.html explicitly — avoids index.html naming collision."""
    return FileResponse(str(FRONTEND / "annotator.html"), media_type="text/html")

# Mount for static assets; html=False so StaticFiles never auto-serves index.html
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="frontend")
