"""기존 API 계약 회귀: 디지털 트윈 확장 전 스냅샷의 모든 오퍼레이션이 그대로 있어야 한다."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "docs" / "baseline" / "api_baseline_2026-10-11.json"


def test_baseline_operations_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKERLESS_DOCS", "1")
    from app.main import create_app

    app = create_app(database_url=f"sqlite:///{tmp_path}/a.db", storage_dir=str(tmp_path / "s"))
    spec = app.openapi()
    now = {f"{m.upper()} {p}" for p, v in spec["paths"].items() for m in v if m in ("get", "post", "put", "patch", "delete")}
    base = set(json.loads(BASE.read_text())["operations"])
    assert not (base - now), f"삭제/변경된 기존 API: {sorted(base - now)}"
