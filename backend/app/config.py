"""앱 설정. 환경변수 접두어 MARKERLESS_ (예: MARKERLESS_DATABASE_URL)."""

from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        self.database_url = os.environ.get(
            "MARKERLESS_DATABASE_URL", "sqlite:///./markerless.db"
        )
        self.storage_dir = os.environ.get("MARKERLESS_STORAGE_DIR", "./storage")
        # 신규 프로젝트/자산의 기본 공차 (현장 Rule 미정 시) — mm/deg
        self.default_position_tolerance_mm = float(
            os.environ.get("MARKERLESS_DEFAULT_POS_TOL_MM", "15")
        )
        self.default_z_tolerance_mm = float(
            os.environ.get("MARKERLESS_DEFAULT_Z_TOL_MM", "10")
        )
        self.default_rotation_tolerance_deg = float(
            os.environ.get("MARKERLESS_DEFAULT_ROT_TOL_DEG", "1.0")
        )
        # ICP 업로드 클라우드 상한 (기기에서 서브샘플 후 전송) — 메모리/응답시간 균형
        self.max_cloud_points = int(os.environ.get("MARKERLESS_MAX_CLOUD_POINTS", "3000"))


settings = Settings()
