"""파일 저장소 — 증거 원본 보존 (원칙 13), GLB/월드맵/메시 샘플 캐시.

URI는 storage 루트 기준 상대경로다. sha256을 파일명에 사용해 중복을 피하고
무결성을 검증 가능하게 한다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _resolve(self, uri: str) -> Path:
        # 경로 탈출 방지
        rel = Path(uri)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"invalid storage uri: {uri}")
        return self.root / rel

    def save(self, subdir: str, data: bytes, suffix: str = "") -> tuple[str, str, int]:
        """저장 후 (uri, sha256, size) 반환."""
        digest = sha256_hex(data)
        name = digest + suffix
        d = self.root / subdir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_bytes(data)
        return f"{subdir}/{name}", digest, len(data)

    def open_bytes(self, uri: str) -> bytes:
        return self._resolve(uri).read_bytes()

    def open_path(self, uri: str) -> Path:
        return self._resolve(uri)

    def delete(self, uri: str) -> bool:
        """파일 삭제 (없으면 False). 같은 sha256 파일을 다른 레코드가 쓰는지는 호출자가 확인한다."""
        path = self._resolve(uri)
        if path.exists():
            path.unlink()
            return True
        return False
