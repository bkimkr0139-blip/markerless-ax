"""FastAPI 앱 팩토리.

실행: cd backend && ../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hmac
import os
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import devlog
from .config import settings
from . import auth
from .db import Base, ensure_schema, make_engine_and_session
from .routers import (
    admin,
    admin_web,
    assets,
    debug,
    evidence,
    health,
    measurements,
    observations,
    packages,
    projects,
    reports,
    results,
    scan,
    spaces,
    tasks,
    twin,
    admin_twin,
)
from . import models_twin  # noqa: F401  (테이블 등록)
from .storage import FileStorage


def create_app(
    *, database_url: str | None = None, storage_dir: str | None = None
) -> FastAPI:
    db_url = database_url or settings.database_url
    root = storage_dir or settings.storage_dir
    engine, session_factory = make_engine_and_session(db_url)
    storage = FileStorage(root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        for sub in ("assets", "cache", "evidence", "worldmaps", "photos", "reviews", "scans", "twin", "cad"):
            (storage.root / sub).mkdir(parents=True, exist_ok=True)
        added = ensure_schema(engine)  # 기존 DB에 뒤늦게 생긴 컬럼 보강
        if added:
            devlog.write("server", "INFO", "schema", f"컬럼 추가: {', '.join(added)}")
        with session_factory() as db:
            n_weak = auth.flag_weak_accounts(db)
            if n_weak:
                devlog.write("server", "WARN", "auth", f"약한/기본 비밀번호 계정 {n_weak}개 — 다음 로그인 때 변경을 강제합니다")
            if not os.environ.get("MARKERLESS_API_KEY"):
                devlog.write("server", "WARN", "auth", "MARKERLESS_API_KEY 미설정 — /api/* 가 인증 없이 열려 있습니다 (외부 공개 시 필수)")
            if auth.seed_admin(db):  # 계정이 없으면 기본 통합관리자 생성 (환경변수로 변경)
                devlog.write("server", "WARN", "auth", "기본 관리자 계정 생성 — 운영 전 비밀번호를 변경하세요")
        yield

    # 자동 API 문서는 외부(터널)에서 API 구조를 그대로 보여 주므로 기본으로 끈다 (개발 시 MARKERLESS_DOCS=1)
    docs_on = os.environ.get("MARKERLESS_DOCS") == "1"
    app = FastAPI(
        title="Markerless BIM/CAD Inspection PoC",
        version="0.1.0",
        description="Mac Studio + 스마트폰 마커리스 시공검수 PoC 백엔드",
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = storage
    from .services.twin import TwinWorker

    app.state.twin_worker = TwinWorker(session_factory, storage)

    # PoC: 사내 망 전제 CORS 오픈 (20절 보안은 파일럿 전환 시 강화)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 서비스 레이어의 ValueError(=데이터/규칙 오류) → 422 BIM 데이터 오류 보고 (12.2)
    @app.exception_handler(ValueError)
    async def _value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # 외부 공개(터널) 대비 API 키 인증: MARKERLESS_API_KEY가 설정되면 /api/* 는 X-API-Key 헤더 필수.
    # /healthz는 열어 둔다 (연결 확인용). 키가 비어 있으면 인증 없음(로컬 개발).
    @app.middleware("http")
    async def _api_key_middleware(request: Request, call_next):
        key = os.environ.get("MARKERLESS_API_KEY", "")
        if key and request.url.path.startswith("/api/") and request.method != "OPTIONS":
            given = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(given.encode(), key.encode()):
                devlog.write("server", "ERROR", "auth", f"{request.method} {request.url.path}",
                             status=401, client=request.client.host if request.client else None,
                             hasKey=bool(given))
                return JSONResponse(status_code=401, content={"detail": "invalid or missing API key"})
        return await call_next(request)

    # 개발 폐루프: 요청 로그 + 4xx/5xx 응답 본문 + 예외 스택 → backend/logs/dev.log
    @app.middleware("http")
    async def _devlog_middleware(request: Request, call_next):
        if not devlog.enabled() or request.url.path in ("/healthz", "/api/v1/debug/log"):
            return await call_next(request)
        t0 = time.perf_counter()
        label = f"{request.method} {request.url.path}"
        try:
            resp = await call_next(request)
        except Exception:
            devlog.write("server", "ERROR", "exception", label, trace=traceback.format_exc()[-3000:])
            return JSONResponse(status_code=500, content={"detail": "internal server error"})
        ms = round((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            body = b"".join([c async for c in resp.body_iterator])
            devlog.write("server", "ERROR", "http", label, status=resp.status_code, ms=ms,
                         body=body[:1500].decode("utf-8", "replace"))
            return Response(content=body, status_code=resp.status_code,
                            headers=dict(resp.headers), media_type=resp.media_type)
        devlog.write("server", "INFO", "http", label, status=resp.status_code, ms=ms)
        return resp

    # 백오피스: 보안 헤더(CSP·프레임 금지·캐시 금지) — 로그인 세션을 쓰는 /admin 에만 적용
    @app.middleware("http")
    async def _admin_security_headers(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/admin"):
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self' blob: data:; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'",
            )
            if request.url.path.startswith("/admin/api"):
                resp.headers["Cache-Control"] = "no-store"
            elif request.url.path.startswith("/admin/static"):
                resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    app.include_router(health.router)
    app.include_router(debug.router)
    app.include_router(admin.router)
    app.include_router(admin_web.router)
    admin_web.mount_static(app)
    app.include_router(projects.router)
    app.include_router(spaces.router)
    app.include_router(assets.router)
    app.include_router(scan.router)
    app.include_router(packages.router)
    app.include_router(tasks.router)
    app.include_router(observations.router)
    app.include_router(measurements.router)
    app.include_router(results.router)
    app.include_router(reports.router)
    app.include_router(evidence.router)
    app.include_router(twin.router)
    app.include_router(admin_twin.router)

    return app


app = create_app()
