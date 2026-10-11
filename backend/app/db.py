from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine_and_session(
    database_url: str,
) -> tuple[object, sessionmaker[Session]]:
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # 기본 풀(5+10)은 3D 화면이 사진 카드 64장을 한꺼번에 요청하면 바닥난다(QueuePool limit reached → 500).
        # SQLite 연결은 가볍다 — 넉넉히 둔다.
        kwargs["pool_size"] = 30
        kwargs["max_overflow"] = 60
        kwargs["pool_timeout"] = 60
    engine = create_engine(database_url, **kwargs)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    from .events import install_change_hooks

    install_change_hooks(session_factory)  # 저장(커밋) 시점에 변경 이벤트 발행 — 앱·관리자 화면이 즉시 반영한다
    return engine, session_factory


# 기존 DB에 뒤늦게 추가된 컬럼 (create_all은 기존 테이블에 컬럼을 추가하지 않는다)
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "spaces": {"updated_at": "DATETIME", "building_id": "INTEGER", "floor_id": "INTEGER", "t_building_space": "JSON",
               "kind": "VARCHAR(16) NOT NULL DEFAULT 'ROOM'"},
    "users": {"must_change_password": "BOOLEAN NOT NULL DEFAULT 0"},
    "assets": {
        "name": "VARCHAR(200) NOT NULL DEFAULT ''",
        "notes": "TEXT NOT NULL DEFAULT ''",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'",
        "device_id": "VARCHAR(128)",
        "updated_at": "DATETIME",
        "scanned_at": "DATETIME",
        "geo": "JSON",
        "inspect_view": "JSON",
    },
}


def ensure_schema(engine) -> list[str]:
    """누락 컬럼을 ALTER TABLE ADD COLUMN으로 보강한다. 추가한 컬럼 목록 반환."""
    from sqlalchemy import inspect, text

    added: list[str] = []
    insp = inspect(engine)
    for table, cols in _ADDED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for col, ddl in cols.items():
            if col not in have:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added.append(f"{table}.{col}")
    return added
