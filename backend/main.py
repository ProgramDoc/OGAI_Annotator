"""
OGAI Annotation Platform — FastAPI backend
backend/main.py  (v1.3)
"""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", Path(__file__).parent.parent))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
FRONTEND   = Path(__file__).parent.parent / "frontend"

PAPERS_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS papers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT    NOT NULL,
                sha256     TEXT    NOT NULL UNIQUE,
                created_at TEXT    DEFAULT (datetime('now')),
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS annotations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id     INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                reviewer_id  TEXT    NOT NULL,
                data_json    TEXT    NOT NULL DEFAULT '{}',
                updated_at   TEXT    DEFAULT (datetime('now')),
                UNIQUE(paper_id, reviewer_id)
            );

            CREATE TABLE IF NOT EXISTS spans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                reviewer_id TEXT    NOT NULL,
                field_name  TEXT    NOT NULL,
                page        INTEGER,
                span_text   TEXT,
                x0 REAL, y0 REAL, x1 REAL, y1 REAL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );
        """)

        for ddl in [
            "ALTER TABLE papers ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
            "ALTER TABLE annotations ADD COLUMN correction_notes TEXT",
            "ALTER TABLE annotations ADD COLUMN corrections_json TEXT",
            "ALTER TABLE annotations ADD COLUMN pipeline_predictions_json TEXT",
        ]:
            try:
                db.execute(ddl)
            except sqlite3.OperationalError:
                pass

        db.commit()


init_db()

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="OGAI Annotation Platform")


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(FRONTEND / "annotator.html"), media_type="text/html")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="frontend")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────
class AnnotationSave(BaseModel):
    reviewer_id: str
    data: dict[str, Any] = {}
    spans: list[dict[str, Any]] = []

class ProjectCreate(BaseModel):
    name: str

class ProjectRename(BaseModel):
    name: str

class PaperAssign(BaseModel):
    project_id: Optional[int] = None

class PrefillRequest(BaseModel):
    study_type: str


# ─────────────────────────────────────────────────────────────────────────────
# Projects CRUD
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/projects")
def list_projects():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at FROM projects ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    with get_db() as db:
        cur = db.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        proj_id = cur.lastrowid
        db.commit()
    return {"id": proj_id, "name": name}


@app.put("/api/projects/{project_id}")
def rename_project(project_id: int, body: ProjectRename):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    with get_db() as db:
        rowcount = db.execute(
            "UPDATE projects SET name=? WHERE id=?", (name, project_id)
        ).rowcount
        db.commit()
    if not rowcount:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"id": project_id, "name": name}


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: int):
    with get_db() as db:
        db.execute("UPDATE papers SET project_id=NULL WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        db.commit()
    return Response(status_code=204)


# ─────────────────────────────────────────────────────────────────────────────
# Papers
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/papers")
def list_papers():
    with get_db() as db:
        papers = db.execute(
            "SELECT id, filename, sha256, project_id FROM papers ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for p in papers:
            annotators = db.execute(
                "SELECT reviewer_id FROM annotations WHERE paper_id=?", (p["id"],)
            ).fetchall()
            result.append({
                "id": p["id"],
                "filename": p["filename"],
                "sha256": p["sha256"],
                "project_id": p["project_id"],
                "annotated_by": [a["reviewer_id"] for a in annotators],
            })
    return result


@app.post("/api/papers/upload")
async def upload_paper(file: UploadFile = File(...)):
    content = await file.read()
    sha = hashlib.sha256(content).hexdigest()
    dest = PAPERS_DIR / f"{sha[:16]}.pdf"
    if not dest.exists():
        dest.write_bytes(content)
    with get_db() as db:
        existing = db.execute("SELECT id FROM papers WHERE sha256=?", (sha,)).fetchone()
        if existing:
            return {"id": existing["id"], "filename": file.filename, "sha256": sha, "new": False}
        cur = db.execute(
            "INSERT INTO papers (filename, sha256) VALUES (?, ?)", (file.filename, sha)
        )
        db.commit()
        return {"id": cur.lastrowid, "filename": file.filename, "sha256": sha, "new": True}


@app.get("/api/papers/{paper_id}/pdf")
def get_paper_pdf(paper_id: int):
    with get_db() as db:
        row = db.execute("SELECT sha256 FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    path = PAPERS_DIR / f"{row['sha256'][:16]}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing")
    return FileResponse(str(path), media_type="application/pdf")


@app.delete("/api/papers/{paper_id}", status_code=204)
def delete_paper(paper_id: int):
    with get_db() as db:
        row = db.execute("SELECT sha256 FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        sha = row["sha256"]
        db.execute("DELETE FROM spans       WHERE paper_id=?", (paper_id,))
        db.execute("DELETE FROM annotations WHERE paper_id=?", (paper_id,))
        db.execute("DELETE FROM papers      WHERE id=?",       (paper_id,))
        db.commit()
    pdf = PAPERS_DIR / f"{sha[:16]}.pdf"
    if pdf.exists():
        pdf.unlink()
    return Response(status_code=204)


@app.post("/api/papers/{paper_id}/assign")
def assign_paper(paper_id: int, body: PaperAssign):
    with get_db() as db:
        db.execute(
            "UPDATE papers SET project_id=? WHERE id=?", (body.project_id, paper_id)
        )
        db.commit()
    return {"paper_id": paper_id, "project_id": body.project_id}


# ─────────────────────────────────────────────────────────────────────────────
# Annotations
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/papers/{paper_id}/annotations")
def get_annotations(paper_id: int, reviewer_id: str = ""):
    with get_db() as db:
        if reviewer_id:
            anns = db.execute(
                "SELECT reviewer_id, data_json, updated_at FROM annotations "
                "WHERE paper_id=? AND reviewer_id=?",
                (paper_id, reviewer_id),
            ).fetchall()
        else:
            anns = db.execute(
                "SELECT reviewer_id, data_json, updated_at FROM annotations WHERE paper_id=?",
                (paper_id,),
            ).fetchall()
        spans = db.execute(
            "SELECT reviewer_id, field_name, page, span_text, x0, y0, x1, y1 "
            "FROM spans WHERE paper_id=?",
            (paper_id,),
        ).fetchall()

    return {
        "annotations": [
            {"reviewer_id": a["reviewer_id"], "data": json.loads(a["data_json"]), "updated_at": a["updated_at"]}
            for a in anns
        ],
        "spans": [dict(s) for s in spans],
    }


@app.post("/api/papers/{paper_id}/annotations")
def save_annotation(paper_id: int, body: AnnotationSave):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            """INSERT INTO annotations (paper_id, reviewer_id, data_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(paper_id, reviewer_id)
               DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at""",
            (paper_id, body.reviewer_id, json.dumps(body.data), now),
        )
        db.execute(
            "DELETE FROM spans WHERE paper_id=? AND reviewer_id=?",
            (paper_id, body.reviewer_id),
        )
        for s in body.spans:
            db.execute(
                """INSERT INTO spans
                   (paper_id, reviewer_id, field_name, page, span_text, x0, y0, x1, y1)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (paper_id, body.reviewer_id,
                 s.get("field_name"), s.get("page"),
                 s.get("text"), s.get("x0"), s.get("y0"), s.get("x1"), s.get("y1")),
            )
        db.commit()
    return {"ok": True, "timestamp": now}


# ─────────────────────────────────────────────────────────────────────────────
# AI Auto-fill
# ─────────────────────────────────────────────────────────────────────────────
EXTRACTION_PROMPT = """\
You are a clinical research data extractor. Extract fields from this PDF and return ONLY a valid JSON object — no markdown fences, no explanation, no preamble.

Study type: {study_type}

Return a flat JSON object with these field IDs as keys. Use "" for any field you cannot find. Never invent values.

Fields to extract:
citation_authors, citation_year, citation_title, citation_journal, citation_doi,
study_objective, population_participants, population_intervention_exposure,
population_comparator, population_outcomes, sample_size_total, sample_size_per_group,
power_calculation_reported, setting, country_region,
study_period_enrollment_start, study_period_enrollment_end, follow_up_duration,
primary_outcome_definition, primary_outcome_measurement, primary_outcome_timing,
secondary_outcomes, key_findings_effect_estimate, key_findings_metric,
key_findings_ci_lower, key_findings_ci_upper, key_findings_pvalue,
key_findings_direction, funding_source, conflicts_of_interest,
limitations_stated, protocol_registration,
clinical_trial_phase, regulatory_context, registration_number,
industry_sponsored, data_source_type, database_name, adaptive_design,
pragmatic_vs_explanatory, trial_framework, target_trial_emulation, pilot_or_feasibility

Return ONLY the JSON object, starting with {{ and ending with }}."""


def _do_prefill(pdf_bytes: bytes, study_type: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NO_API_KEY")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                },
                {"type": "text", "text": EXTRACTION_PROMPT.format(study_type=study_type)},
            ],
        }],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "pdfs-2024-09-25",
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API {e.code}: {body}")

    text = result["content"][0]["text"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(text)


@app.post("/api/papers/{paper_id}/prefill")
async def prefill_fields(paper_id: int, body: PrefillRequest):
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not set. Add it in Render → Environment.",
        )

    with get_db() as db:
        row = db.execute("SELECT sha256 FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")

    pdf_path = PAPERS_DIR / f"{row['sha256'][:16]}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")

    try:
        predictions = await asyncio.to_thread(_do_prefill, pdf_path.read_bytes(), body.study_type)
    except RuntimeError as e:
        msg = str(e)
        if "NO_API_KEY" in msg:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
        raise HTTPException(status_code=502, detail=msg)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Model returned non-JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Extraction failed: {e}")

    return predictions


# ─────────────────────────────────────────────────────────────────────────────
# CSV Export
# ─────────────────────────────────────────────────────────────────────────────
FLAT_COLS = [
    "major_category", "subcategory", "study_type",
    "rule1_pass", "rule2_pass", "rule2b_pass", "rule3_pass",
    "natural_experiment_flag", "author_stated_design", "author_label_discordance",
    "reviewer_action",
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
    "key_findings_ci_lower", "key_findings_ci_upper", "key_findings_pvalue",
    "key_findings_direction",
    "funding_source", "conflicts_of_interest", "limitations_stated", "protocol_registration",
    "randomization_method", "allocation_concealment", "allocation_ratio",
    "stratification_factors", "baseline_balance",
    "blinding_participants", "blinding_personnel", "blinding_outcome_assessors",
    "protocol_deviations", "analysis_framework", "attrition_rate",
    "missing_data_handling", "outcome_measurement_method",
    "protocol_available", "outcomes_match_protocol", "consort_flow_diagram",
    "cluster_unit", "n_clusters", "icc_reported",
    "recruitment_after_randomization", "clustering_in_analysis", "contamination_risk",
    "washout_period", "carryover_assessment", "period_effects", "sequence_order", "paired_analysis",
    "concurrent_control_confirmed", "allocation_mechanism",
    "baseline_comparability", "confounding_control", "blinding",
    "primary_endpoint_prespecified", "inclusion_exclusion_criteria",
    "comparator_historical_reference", "consecutive_enrolment",
    "escalation_scheme", "dlt_definition", "dose_levels",
    "mtd_declared", "rp2d", "expansion_cohort", "pk_pd_reported",
    "n_data_points_pre", "n_data_points_post", "intervention_date",
    "control_series", "statistical_method", "level_change", "slope_change",
    "autocorrelation_addressed", "seasonality_adjustment", "concurrent_events",
    "exogenous_event", "parallel_trends_evidence", "n_pre_period_points",
    "interaction_term", "common_shocks", "staggered_adoption",
    "running_variable", "cutoff_value", "sharp_vs_fuzzy",
    "bandwidth_selection", "manipulation_testing", "continuity_plots",
    "exposure_definition", "exposure_measurement", "comparator_group",
    "outcome_ascertainment", "confounders_measured", "adjustment_method",
    "loss_to_follow_up", "immortal_time_bias",
    "case_definition", "case_source", "control_selection",
    "matching", "exposure_ascertainment", "recall_bias_risk",
    "case_definition_ccx", "exposure_definition_ccx",
    "hazard_period", "control_period", "induction_period",
    "temporal_direction", "exposure_variability", "conditional_logistic", "self_selection_bias",
    "index_test", "reference_standard",
    "blinding_index_to_reference", "blinding_reference_to_index",
    "two_by_two_table", "spectrum_of_patients", "verification_bias",
    "threshold_effects", "flow_and_timing",
    "prognostic_factor", "outcome_definition",
    "study_participation", "study_attrition", "pf_measurement", "statistical_analysis",
    "predictors_candidate", "predictor_selection_method", "model_type",
    "discrimination", "calibration", "model_presentation", "model_stage",
    "search_strategy", "inclusion_criteria", "study_selection", "data_extraction",
    "included_studies_n", "rob_tool_used", "synthesis_method",
    "grade_assessment", "prisma_flow",
    "effect_measure", "pooled_estimate", "pooling_model",
    "heterogeneity", "publication_bias", "sensitivity_analyses", "subgroup_analyses",
    "guideline_organization", "panel_composition", "evidence_base",
    "grade_used", "recommendations", "updating_plan",
    "methodology", "data_collection", "sampling_strategy",
    "data_saturation", "reflexivity", "themes",
    "evaluation_type", "perspective", "time_horizon", "discount_rate",
    "cost_inputs", "effectiveness_source", "icer", "sensitivity_analysis",
    "clinical_trial_phase", "regulatory_context", "registration_number",
    "industry_sponsored", "data_source_type", "database_name",
    "adaptive_design", "pragmatic_vs_explanatory", "trial_framework",
    "target_trial_emulation", "pilot_or_feasibility",
    "correction_notes", "corrections_json", "pipeline_predictions_json",
]


def _csv_escape(val: str) -> str:
    val = str(val).replace('"', '""')
    if any(c in val for c in (',', '"', '\n', '\r')):
        val = f'"{val}"'
    return val


@app.get("/api/export/csv")
def export_csv():
    with get_db() as db:
        papers = db.execute(
            "SELECT id, filename, project_id FROM papers ORDER BY created_at ASC"
        ).fetchall()
        projects = {r["id"]: r["name"] for r in
                    db.execute("SELECT id, name FROM projects").fetchall()}
        rows = []
        for paper in papers:
            anns = db.execute(
                "SELECT reviewer_id, data_json, updated_at FROM annotations WHERE paper_id=?",
                (paper["id"],),
            ).fetchall()
            for ann in anns:
                data = json.loads(ann["data_json"])
                row = {
                    "filename": paper["filename"],
                    "paper_id": str(paper["id"]),
                    "project": projects.get(paper["project_id"], ""),
                    "reviewer_id": ann["reviewer_id"],
                    "updated_at": ann["updated_at"],
                }
                for col in FLAT_COLS:
                    row[col] = data.get(col, "")
                rows.append(row)

    headers = ["filename", "paper_id", "project", "reviewer_id", "updated_at"] + FLAT_COLS

    def generate():
        yield ",".join(headers) + "\r\n"
        for row in rows:
            yield ",".join(_csv_escape(row.get(h, "")) for h in headers) + "\r\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ogai_annotations.csv"},
    )
