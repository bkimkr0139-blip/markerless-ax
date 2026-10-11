"""변경 이벤트 — 저장(커밋)이 일어난 시점에 연결된 단말(앱)·관리자 화면에 알린다. 주기 확인(polling)은 쓰지 않는다.

흐름: DB 커밋 → (db.py 훅) broker.publish({"assets", ...}) → 0.35초 안의 연속 변경은 한 번으로 합쳐 → 구독 중인 SSE 연결들에 전달.
- 단말: GET /api/v1/sync/events  (X-API-Key)  — 공간·자산이 바뀌면 앱이 그때 한 번 새로 읽는다
- 관리자: GET /admin/api/events  (로그인 세션) — 모바일에서 등록·검수한 내용이 화면에 바로 나타난다
스코프: assets · spaces · inspections · issues
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi.concurrency import run_in_threadpool
from fastapi import Request

# 변경을 감시하는 모델 → 스코프
MODEL_SCOPES = {
    "Asset": "assets", "Space": "spaces", "Observation": "inspections", "InspectionResult": "inspections",
    "Issue": "issues", "IssueEvent": "issues",
    # 디지털 트윈: 상태·리비전만 알린다 (고용량 메시·사진은 이벤트에 싣지 않는다)
    "ScanSession": "twin", "MeshRevision": "twin", "Wall": "twin", "SpaceConnector": "twin", "Building": "twin",
    "CadReplacement": "twin", "Opening": "twin",
}


class EventBroker:
    def __init__(self, coalesce_s: float = 0.35) -> None:
        self._listeners: list[Callable[[set[str]], None]] = []
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None
        self.coalesce_s = coalesce_s

    def add_listener(self, fn: Callable[[set[str]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return remove

    def publish(self, scopes: set[str]) -> None:
        """어느 스레드에서든 호출 가능. 연속 변경은 coalesce_s 안에서 한 번으로 합친다."""
        if not scopes:
            return
        with self._lock:
            self._pending |= set(scopes)
            if self._timer is None:
                self._timer = threading.Timer(self.coalesce_s, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            scopes, self._pending, self._timer = self._pending, set(), None
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(set(scopes))
            except Exception:  # 한 구독자의 오류가 다른 구독자를 막지 않게
                pass


broker = EventBroker()


def install_change_hooks(session_factory) -> None:
    """어떤 경로로든 Asset/Space/관측/이슈가 커밋되면 이벤트를 낸다 (엔드포인트마다 publish를 넣지 않아도 된다)."""
    from sqlalchemy import event

    @event.listens_for(session_factory, "after_flush")
    def _after_flush(session, _ctx) -> None:
        scopes = session.info.setdefault("change_scopes", set())
        for obj in (*session.new, *session.dirty, *session.deleted):
            sc = MODEL_SCOPES.get(type(obj).__name__)
            if sc:
                scopes.add(sc)

    @event.listens_for(session_factory, "after_commit")
    def _after_commit(session) -> None:
        scopes = session.info.pop("change_scopes", None)
        if scopes:
            broker.publish(set(scopes))

    @event.listens_for(session_factory, "after_rollback")
    def _after_rollback(session) -> None:
        session.info.pop("change_scopes", None)


async def sse_stream(request: Request, make_payload: Callable[[set[str]], Awaitable[dict]]) -> AsyncIterator[str]:
    """SSE 본문 생성기. 처음에 hello(현재 상태) 한 번, 이후에는 변경이 있을 때만 보낸다.
    20초 동안 변경이 없으면 연결 유지용 주석(': ping')만 보낸다 — 데이터 확인이 아니라 프록시가 연결을 끊지 않게 하는 용도."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[set[str]] = asyncio.Queue()
    remove = broker.add_listener(lambda s: loop.call_soon_threadsafe(q.put_nowait, s))
    try:
        yield "retry: 3000\n\n"
        yield f"data: {json.dumps({**await make_payload(set()), 'type': 'hello'}, ensure_ascii=False)}\n\n"
        while True:
            try:
                scopes = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    break
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps({**await make_payload(scopes), 'type': 'change'}, ensure_ascii=False)}\n\n"
    finally:
        remove()


SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
