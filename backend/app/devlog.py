"""개발 폐루프용 로그 (MARKERLESS_DEBUG=1 일 때만 활성, `make api`가 설정).

서버 요청/오류와 앱(실기기)이 보낸 로그를 한 파일(backend/logs/dev.log, JSONL)에 모아
개발자(또는 Claude)가 tail 해서 원인 분석 → 수정 → 재배포 루프를 돌릴 수 있게 한다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "dev.log"
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("MARKERLESS_DEBUG") == "1"


def write(source: str, level: str, tag: str, message: str, **extra: object) -> None:
    if not enabled():
        return
    rec = {
        "ts": time.strftime("%H:%M:%S"),
        "src": source,
        "lvl": level,
        "tag": tag,
        "msg": message,
        **{k: v for k, v in extra.items() if v not in (None, "", {})},
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
