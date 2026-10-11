"""현장 검수 문제 처리 워크플로우 (이슈).

흐름:  발견(OPEN) → 확인(CONFIRMED) → 조치 지시(ASSIGNED) → 조치 중(IN_PROGRESS) → 조치 완료(RESOLVED)
       → 재검수 요청(REINSPECT) → 종결(CLOSED)         ※ 오탐/무시는 DISMISSED

- 검수 결과가 FAIL/REVIEW면 이슈가 자동 생성된다 (같은 자산에 열린 이슈가 있으면 '재측정' 이력만 추가).
- 종결은 재검수 PASS가 있어야 한다 (없으면 관리자/현장관리자의 사유 있는 강제 종결).
- 모든 전이는 IssueEvent로 남는다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, Issue, IssueEvent, Observation, User

STATUS_LABEL = {
    "OPEN": "발견", "CONFIRMED": "확인됨", "ASSIGNED": "조치 지시", "IN_PROGRESS": "조치 중",
    "RESOLVED": "조치 완료", "REINSPECT": "재검수 요청", "CLOSED": "종결", "DISMISSED": "오탐·무시",
}
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
SEVERITY_LABEL = {"LOW": "경미", "MEDIUM": "보통", "HIGH": "심각", "CRITICAL": "치명"}
DECISION_LABEL = {"PASS": "합격", "FAIL": "불합격", "REVIEW": "사람 확인", "RETAKE": "재측정"}
OPEN_STATUSES = ("OPEN", "CONFIRMED", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "REINSPECT")

# from → to → 허용 역할 ("assignee"는 담당자 본인). viewer는 어떤 전이도 불가.
TRANSITIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "OPEN": {"CONFIRMED": ("inspector", "manager", "admin"), "DISMISSED": ("inspector", "manager", "admin")},
    "CONFIRMED": {"ASSIGNED": ("manager", "admin"), "DISMISSED": ("inspector", "manager", "admin"),
                  "OPEN": ("inspector", "manager", "admin")},
    "ASSIGNED": {"IN_PROGRESS": ("manager", "admin", "assignee"), "CONFIRMED": ("manager", "admin")},
    "IN_PROGRESS": {"RESOLVED": ("manager", "admin", "assignee")},
    "RESOLVED": {"REINSPECT": ("inspector", "manager", "admin"), "IN_PROGRESS": ("inspector", "manager", "admin")},
    "REINSPECT": {"CLOSED": ("inspector", "manager", "admin"), "IN_PROGRESS": ("inspector", "manager", "admin")},
    "CLOSED": {"OPEN": ("admin",)},
    "DISMISSED": {"OPEN": ("manager", "admin")},
}


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def code(issue: Issue) -> str:
    return f"ISS-{issue.id:04d}"


def allowed_transitions(issue: Issue, user: User) -> list[str]:
    """이 사용자가 지금 이 이슈에서 실행할 수 있는 전이."""
    out = []
    for to, roles in TRANSITIONS.get(issue.status, {}).items():
        if user.role in roles or ("assignee" in roles and issue.assignee == user.username):
            out.append(to)
    return out


def add_event(db: Session, issue: Issue, actor: str, type_: str, comment: str = "",
              from_status: str | None = None, to_status: str | None = None, data: dict | None = None) -> IssueEvent:
    ev = IssueEvent(issue_id=issue.id, actor=actor, type=type_, comment=comment,
                    from_status=from_status, to_status=to_status, data=data or {})
    db.add(ev)
    issue.updated_at = _now()
    return ev


def create_issue(db: Session, *, asset_id: str | None, observation_id: str | None, title: str, description: str,
                 severity: str, reporter: str, source: str = "manual", assignee: str | None = None,
                 due_date: str | None = None, space_id: int | None = None) -> Issue:
    if severity not in SEVERITIES:
        raise WorkflowError(f"severity must be one of {SEVERITIES}")
    if asset_id is not None and db.get(Asset, asset_id) is None:
        raise WorkflowError(f"asset not found: {asset_id}", 404)
    if space_id is None and asset_id is not None:
        space_id = db.get(Asset, asset_id).space_id
    issue = Issue(asset_id=asset_id, observation_id=observation_id, space_id=space_id, title=title.strip()[:300],
                  description=description, severity=severity, reporter=reporter, source=source,
                  assignee=assignee, due_date=due_date)
    db.add(issue)
    db.flush()
    add_event(db, issue, reporter, "created", comment=description[:500], to_status="OPEN",
              data={"source": source, "observationId": observation_id})
    return issue


def _validate_assignee(db: Session, username: str | None) -> str:
    if not username:
        raise WorkflowError("조치 담당자를 지정해야 합니다")
    u = db.scalar(select(User).where(User.username == username))
    if u is None or not u.active or u.role == "viewer":
        raise WorkflowError(f"담당자로 지정할 수 없는 계정입니다: {username}")
    return u.username


def _has_reinspect_pass(db: Session, issue: Issue) -> bool:
    """조치 완료 이후 같은 자산의 PASS 검수가 있는가 (재검수 근거)."""
    if issue.asset_id is None:
        return False
    resolved_at = None
    for ev in db.scalars(select(IssueEvent).where(IssueEvent.issue_id == issue.id).order_by(IssueEvent.id)).all():
        if ev.to_status == "RESOLVED":
            resolved_at = _aware(ev.at)
    if resolved_at is None:
        return False
    for obs in db.scalars(select(Observation).where(Observation.asset_id == issue.asset_id,
                                                    Observation.decision == "PASS")).all():
        if _aware(obs.created_at) and _aware(obs.created_at) > resolved_at:
            return True
    return False


def transition(db: Session, issue: Issue, user: User, to: str, *, comment: str = "", assignee: str | None = None,
               due_date: str | None = None, resolution: str | None = None, force: bool = False) -> Issue:
    if to not in TRANSITIONS.get(issue.status, {}):
        raise WorkflowError(f"'{STATUS_LABEL[issue.status]}'에서 '{STATUS_LABEL.get(to, to)}'(으)로 변경할 수 없습니다")
    if to not in allowed_transitions(issue, user):
        raise WorkflowError(f"이 단계는 권한이 없습니다 ({user.role})", 403)

    data: dict = {}
    if to == "ASSIGNED":
        issue.assignee = _validate_assignee(db, assignee or issue.assignee)
        if due_date:
            issue.due_date = due_date
        data = {"assignee": issue.assignee, "dueDate": issue.due_date}
    elif to == "DISMISSED":
        if not comment.strip():
            raise WorkflowError("오탐·무시 사유를 입력해야 합니다")
    elif to == "RESOLVED":
        text = (resolution or comment).strip()
        if not text:
            raise WorkflowError("조치 내용을 입력해야 합니다")
        issue.resolution = text
        data = {"resolution": text}
    elif to == "CLOSED":
        if _has_reinspect_pass(db, issue):
            data = {"reinspectPass": True}
        else:
            # 재검수 PASS 없이 종결하려면 관리자/현장관리자의 사유 있는 강제 종결이어야 한다
            if not (force and comment.strip() and user.role in ("manager", "admin")):
                raise WorkflowError("재검수 합격(PASS) 기록이 없습니다. 먼저 재검수를 하거나, 관리자가 사유와 함께 강제 종결하세요", 409)
            data = {"forced": True}
        issue.closed_at = _now()
    elif to == "OPEN":
        issue.closed_at = None

    prev = issue.status
    issue.status = to
    add_event(db, issue, user.username, "status", comment=comment, from_status=prev, to_status=to, data=data)
    return issue


def assign_or_edit(db: Session, issue: Issue, user: User, *, title: str | None = None, description: str | None = None,
                   severity: str | None = None, assignee: str | None = None, due_date: str | None = None) -> Issue:
    changes: dict = {}
    if title is not None and title.strip() and title != issue.title:
        changes["title"] = [issue.title, title.strip()[:300]]; issue.title = title.strip()[:300]
    if description is not None and description != issue.description:
        changes["description"] = True; issue.description = description
    if severity is not None and severity != issue.severity:
        if severity not in SEVERITIES:
            raise WorkflowError(f"severity must be one of {SEVERITIES}")
        changes["severity"] = [issue.severity, severity]; issue.severity = severity
    if assignee is not None and assignee != (issue.assignee or ""):
        issue.assignee = _validate_assignee(db, assignee) if assignee else None
        changes["assignee"] = issue.assignee
    if due_date is not None and due_date != (issue.due_date or ""):
        changes["dueDate"] = due_date or None; issue.due_date = due_date or None
    if changes:
        add_event(db, issue, user.username, "edit", data=changes)
    return issue


# ───────────────────────── 검수 결과 → 자동 이슈 ─────────────────────────

def _auto_severity(decision: str, deviation: dict | None, tolerance: dict | None) -> str:
    if decision == "REVIEW":
        return "LOW"
    dev = deviation or {}
    tol = tolerance or {}
    pos_ratio = float(dev.get("distMm", 0)) / max(float(tol.get("positionMm", 15) or 15), 1e-6)
    rot_ratio = float(dev.get("maxRotDeg", 0)) / max(float(tol.get("rotationDeg", 1.0) or 1.0), 1e-6)
    worst = max(pos_ratio, rot_ratio)
    if worst >= 6:
        return "CRITICAL"
    if worst >= 3:
        return "HIGH"
    return "MEDIUM"


def on_measurement(db: Session, obs: Observation, decision: str, reasons: list[str], asset: Asset,
                   deviation: dict | None) -> Issue | None:
    """검수 결과를 이슈 워크플로우에 연결한다.

    FAIL/REVIEW → 열린 이슈가 있으면 '재측정' 이력 추가, 없으면 자동 생성.
    PASS        → 열린 이슈가 있으면 '재측정 합격' 이력 (재검수 근거).
    """
    open_issue = db.scalar(select(Issue).where(Issue.asset_id == asset.asset_id, Issue.status.in_(OPEN_STATUSES))
                           .order_by(Issue.id.desc()))
    summary = {"observationId": obs.id, "decision": decision}
    if decision == "PASS":
        if open_issue is not None:
            add_event(db, open_issue, "system", "remeasure",
                      comment=f"재측정 결과 합격 — {reasons[0] if reasons else ''}", data=summary)
        return None
    if decision not in ("FAIL", "REVIEW"):
        return None  # RETAKE 등은 이슈로 만들지 않는다

    dev = deviation or {}
    detail = f"{reasons[0] if reasons else ''}"
    if open_issue is not None:
        add_event(db, open_issue, "system", "remeasure", comment=f"재측정 결과 {DECISION_LABEL.get(decision, decision)} — {detail}",
                  data={**summary, "distMm": dev.get("distMm"), "maxRotDeg": dev.get("maxRotDeg")})
        sev = _auto_severity(decision, deviation, asset.tolerance)
        if SEVERITIES.index(sev) > SEVERITIES.index(open_issue.severity):
            open_issue.severity = sev  # 악화되면 심각도 상향
        return open_issue
    title = f"{asset.name or asset.asset_id} — {DECISION_LABEL.get(decision, decision)}"
    return create_issue(db, asset_id=asset.asset_id, observation_id=obs.id, title=title,
                        description="\n".join(reasons) if reasons else title,
                        severity=_auto_severity(decision, deviation, asset.tolerance), reporter="system", source="auto")
