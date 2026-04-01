"""
AI prefill route: calls Anthropic API to extract structured fields from a PDF.
"""

import asyncio
import base64
import json
import logging
import os

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import PAPERS_DIR
from .db import get_db

logger = logging.getLogger("ogai")
router = APIRouter(prefix="/api/papers", tags=["prefill"])


# ─────────────────────────────────────────────
# Field ID lists (kept in sync with frontend schema)
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


class PrefillRequest(BaseModel):
    study_type: str


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
    """Call the Anthropic API using the SDK (with automatic retries)."""
    try:
        import anthropic
    except ImportError:
        # Fallback to urllib if SDK not installed
        return _call_anthropic_urllib(pdf_bytes, prompt)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")

    text = next((b.text.strip() for b in message.content if b.type == "text"), "")
    if not text:
        raise HTTPException(status_code=502, detail="Empty response from Anthropic API")

    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```")).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"JSON parse error: {e}. Got: {text[:300]}")


def _call_anthropic_urllib(pdf_bytes: bytes, prompt: str) -> dict:
    """Fallback: call Anthropic API via urllib (no SDK dependency)."""
    import urllib.error
    import urllib.request

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


@router.post("/{paper_id}/prefill")
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
