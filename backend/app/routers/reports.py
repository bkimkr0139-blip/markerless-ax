from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import InspectionResult, Observation, Report
from ..schemas import ReportIn, ReportOut

router = APIRouter(prefix="/api/v1", tags=["reports"])


def _summary_line(r: InspectionResult, obs: Observation | None) -> str:
    """Astra가 그대로 읽을 수 있는 한국어 요약 (개발지시서 12.3 형식)."""
    d = r.deviation or {}
    conf = r.confidence
    if r.decision == "RETAKE":
        return f"{r.asset_id}: 측정 신뢰도 부족({conf}) — 재촬영이 필요합니다."
    base = (
        f"{r.asset_id}: 설계 대비 X {d.get('dxMm', 0.0):+.0f}mm, "
        f"Y {d.get('dyMm', 0.0):+.0f}mm, Z {d.get('dzMm', 0.0):+.0f}mm, "
        f"회전 최대 {d.get('maxRotDeg', 0.0):.1f}° (신뢰도 {conf})"
    )
    if r.decision == "PASS":
        return base + " — 공차 이내입니다. PASS."
    if r.decision == "FAIL":
        reason = (r.reasons or [""])[0]
        return base + f" — 허용 공차를 초과했습니다. {reason}"
    return base + " — 사람 확인이 필요합니다. REVIEW."


@router.post("/reports", response_model=ReportOut, status_code=201)
def create_report(body: ReportIn, db: Session = Depends(get_db)) -> ReportOut:
    q = select(InspectionResult).join(
        Observation, InspectionResult.observation_id == Observation.id
    )
    if body.observationIds:
        q = q.where(InspectionResult.observation_id.in_(body.observationIds))
    elif body.taskId is not None:
        q = q.where(Observation.task_id == body.taskId)
    else:
        raise HTTPException(status_code=422, detail="taskId or observationIds required")
    q = q.order_by(InspectionResult.created_at.asc())
    results = db.scalars(q).all()
    if not results:
        raise HTTPException(status_code=422, detail="no inspection results for given scope")

    obs_map = {
        o.id: o
        for o in db.scalars(
            select(Observation).where(
                Observation.id.in_([r.observation_id for r in results])
            )
        ).all()
    }

    counts: dict[str, int] = {}
    for r in results:
        counts[r.decision] = counts.get(r.decision, 0) + 1

    summary = [
        f"검수 대상 {len(results)}건: "
        + ", ".join(f"{k} {v}건" for k, v in sorted(counts.items()))
    ] + [_summary_line(r, obs_map.get(r.observation_id)) for r in results]

    content = {
        "counts": counts,
        "results": [
            {
                "resultId": r.id,
                "assetId": r.asset_id,
                "decision": r.decision,
                "confidence": r.confidence,
                "deviation": r.deviation,
                "reasons": list(r.reasons or []),
                "bimRevision": (obs_map.get(r.observation_id).bim_revision if obs_map.get(r.observation_id) else None),
            }
            for r in results
        ],
    }
    report = Report(task_id=body.taskId, title=body.title, content=content)
    report.content = {**content, "summary": summary}
    db.add(report)
    db.commit()

    return ReportOut(
        reportId=report.id,
        taskId=report.task_id,
        title=report.title,
        summary=summary,
        results=content["results"],
        createdAt=report.created_at.isoformat() if report.created_at else None,
    )


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: str, db: Session = Depends(get_db)) -> ReportOut:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"report not found: {report_id}")
    c = report.content or {}
    return ReportOut(
        reportId=report.id,
        taskId=report.task_id,
        title=report.title,
        summary=list(c.get("summary", [])),
        results=list(c.get("results", [])),
        createdAt=report.created_at.isoformat() if report.created_at else None,
    )
