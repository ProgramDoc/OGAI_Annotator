"""
Annotation routes: save/load annotations and spans.
Includes optimistic concurrency control via version column.
"""

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .db import get_db

router = APIRouter(prefix="/api/papers", tags=["annotations"])


class AnnotationPayload(BaseModel):
    data: dict[str, Any] = {}
    spans: list[dict[str, Any]] = []
    field_annotations: dict[str, Any] = {}
    version: int | None = None  # for optimistic concurrency


@router.get("/{paper_id}/annotations")
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
        # Include version for concurrency control
        version = row["version"] if "version" in row.keys() else 1
        annotations.append({
            "id": row["id"],
            "reviewer_id": row["reviewer_id"],
            "timestamp": row["timestamp"],
            "data": data,
            "version": version,
        })

    spans = conn.execute(
        "SELECT * FROM spans WHERE paper_id=? AND reviewer_id=?",
        (paper_id, reviewer_id),
    ).fetchall()
    conn.close()
    return {"annotations": annotations, "spans": [dict(s) for s in spans]}


@router.post("/{paper_id}/annotations")
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
        # ── Optimistic concurrency check ──
        existing = conn.execute(
            "SELECT version FROM annotations WHERE paper_id=? AND reviewer_id=?",
            (paper_id, reviewer_id),
        ).fetchone()

        if existing and payload.version is not None:
            db_version = existing["version"] if "version" in existing.keys() else 1
            if payload.version < db_version:
                conn.close()
                raise HTTPException(
                    status_code=409,
                    detail="Conflict: this paper was updated elsewhere. Please reload.",
                )

        new_version = (existing["version"] + 1 if existing and "version" in existing.keys() else 2) if existing else 1

        conn.execute(
            """INSERT INTO annotations
                   (paper_id, reviewer_id, data_json, timestamp,
                    correction_notes, corrections_json, pipeline_predictions_json,
                    field_annotations_json, version)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(paper_id, reviewer_id) DO UPDATE SET
                   data_json=excluded.data_json, timestamp=excluded.timestamp,
                   correction_notes=excluded.correction_notes,
                   corrections_json=excluded.corrections_json,
                   pipeline_predictions_json=excluded.pipeline_predictions_json,
                   field_annotations_json=excluded.field_annotations_json,
                   version=excluded.version""",
            (paper_id, reviewer_id, json.dumps(data), now,
             correction_notes, corrections_json, pipeline_predictions_json,
             field_annotations_json, new_version),
        )

        # Span replacement within the same transaction (atomic)
        conn.execute("DELETE FROM spans WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id))
        for s in payload.spans:
            conn.execute(
                "INSERT INTO spans (paper_id, reviewer_id, field_name, page, text, x0, y0, x1, y1) VALUES (?,?,?,?,?,?,?,?,?)",
                (paper_id, reviewer_id, s.get("field_name"), s.get("page"), s.get("text"),
                 s.get("x0"), s.get("y0"), s.get("x1"), s.get("y1")),
            )
        conn.commit()
    conn.close()
    return {"status": "ok", "timestamp": now, "reviewer_id": reviewer_id, "version": new_version}
