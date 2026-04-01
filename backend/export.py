"""
CSV export route (user-scoped).
Includes CSV formula injection protection.
"""

import json
from typing import Optional

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import StreamingResponse

from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/export", tags=["export"])


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

# Characters that trigger formula execution in Excel/Sheets
_FORMULA_CHARS = set("=+-@\t\r")


def _csv_row(vals: list) -> str:
    def _e(v):
        v = str(v)
        # CSV formula injection protection: prefix dangerous first chars with tab
        if v and v[0] in _FORMULA_CHARS:
            v = "\t" + v
        v = v.replace('"', '""')
        return f'"{v}"' if any(c in v for c in (',', '"', '\n', '\r', '\t')) else v
    return ",".join(_e(v) for v in vals) + "\r\n"


def _build_export_rows(
    user_id: int,
    paper_id: Optional[int] = None,
    project_id: Optional[int] = None,
) -> tuple[list[str], str]:
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

    if paper_id is not None and paper_ids:
        fn = f"ogai_{papers[paper_ids[0]]['filename'].replace('.pdf','')}.csv"
    elif project_id is not None:
        fn = f"ogai_project_{proj_names.get(project_id, str(project_id))}.csv".replace(" ", "_")
    else:
        fn = "ogai_annotations.csv"

    return rows, fn


@router.get("/csv")
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
