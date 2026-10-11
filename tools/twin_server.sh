#!/usr/bin/env bash
# 임시 DB/저장소로 디지털 트윈 검증용 서버를 띄운다 (운영 데이터와 분리). 사용: tools/twin_server.sh [포트=8113]
cd "$(dirname "$0")/.."
PORT="${1:-8113}"; D=/tmp/twin_demo; mkdir -p $D
cd backend && MARKERLESS_ENFORCE_PW_CHANGE=0 MARKERLESS_DATABASE_URL="sqlite:///$D/t.db" MARKERLESS_STORAGE_DIR="$D/storage" ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
