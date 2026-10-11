"""백오피스 인증·권한.

- 비밀번호: scrypt(stdlib) + 계정별 salt. 평문은 저장하지 않는다.
- 세션: 무작위 토큰을 DB(auth_sessions)에 저장 — 로그아웃·계정 비활성화·비밀번호 변경 시 즉시 폐기 가능.
  쿠키는 HttpOnly + SameSite=Strict (https 접속이면 Secure).
- 무차별 대입 방지: (IP, 계정) 5회 실패 시 5분 잠금.
- CSRF: 변경 요청(POST/PATCH/PUT/DELETE)은 `X-Requested-With: markerless-admin` 헤더 필수 (SameSite=Strict와 함께).
- 역할: admin(통합관리자) > manager(현장관리자) > inspector(현장 검수자) > viewer(조회 전용)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import get_db
from .models import AuditLog, AuthSession, User

SESSION_COOKIE = "mk_admin"
SESSION_HOURS = 12
MAX_FAILS = 5
LOCK_SECONDS = 300
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "markerless-admin"
PW_EXEMPT = ("/admin/api/me", "/admin/api/me/password", "/admin/api/logout", "/admin/api/session")

ROLE_RANK = {"viewer": 0, "inspector": 1, "manager": 2, "admin": 3}
ROLE_LABEL = {"admin": "통합관리자", "manager": "현장관리자", "inspector": "현장 검수자", "viewer": "조회 전용"}


# ───────────────────────── 비밀번호 ─────────────────────────

def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    _, h = hash_password(password, salt_hex)
    return hmac.compare_digest(h, hash_hex)


WEAK_PASSWORDS = {"admin123!@#", "admin", "password", "12345678", "1234567890", "qwerty123", "admin1234", "markerless", "changeme"}


def password_problems(password: str, username: str = "") -> list[str]:
    """비밀번호 정책: 10자 이상, 영 대/소문자·숫자·기호 중 3종 이상, 아이디 포함·흔한 암호 금지."""
    out = []
    if len(password) < 10:
        out.append("10자 이상이어야 합니다")
    classes = sum(bool(x) for x in (any(c.islower() for c in password), any(c.isupper() for c in password),
                                    any(c.isdigit() for c in password), any(not c.isalnum() for c in password)))
    if classes < 3:
        out.append("영문 대문자·소문자·숫자·기호 중 3가지 이상을 섞어야 합니다")
    low = password.lower()
    if username and len(username) >= 3 and username.lower() in low:
        out.append("아이디를 포함할 수 없습니다")
    if low in WEAK_PASSWORDS or any(w in low for w in ("admin123", "password", "qwerty", "12345678")):
        out.append("흔히 쓰이는 약한 비밀번호입니다")
    return out


def require_strong(password: str, username: str = "") -> None:
    problems = password_problems(password, username)
    if problems:
        raise HTTPException(status_code=422, detail="비밀번호 정책: " + " · ".join(problems))


def enforce_pw_change() -> bool:
    """테스트·로컬 임시 서버는 MARKERLESS_ENFORCE_PW_CHANGE=0 으로 끈다. 기본은 켜짐(외부 공개 서버 보호)."""
    return os.environ.get("MARKERLESS_ENFORCE_PW_CHANGE", "1") != "0"


def flag_weak_accounts(db: Session) -> int:
    """기본/약한 비밀번호를 쓰는 기존 계정에 변경 강제 표시 (알려진 약한 암호 목록과 대조)."""
    n = 0
    for u in db.scalars(select(User)).all():
        if not u.must_change_password and any(verify_password(w, u.pw_salt, u.pw_hash) for w in WEAK_PASSWORDS):
            u.must_change_password = True
            n += 1
    if n:
        db.commit()
    return n


def seed_admin(db: Session) -> bool:
    """계정이 하나도 없으면 기본 통합관리자를 만든다 (환경변수로 변경 가능). 만들었으면 True."""
    if db.scalar(select(User.id).limit(1)) is not None:
        return False
    username = os.environ.get("MARKERLESS_ADMIN_USER", "admin")
    password = os.environ.get("MARKERLESS_ADMIN_PASSWORD", "admin123!@#")
    salt, h = hash_password(password)
    db.add(User(username=username, display_name="통합관리자", role="admin", pw_salt=salt, pw_hash=h,
                must_change_password=bool(password_problems(password, username))))
    db.commit()
    return True


# ───────────────────────── 로그인 시도 제한 ─────────────────────────

_fails: dict[tuple[str, str], tuple[int, float]] = {}  # (ip, username) → (연속 실패, 잠금 해제 시각)


def throttle_check(ip: str, username: str) -> None:
    n, until = _fails.get((ip, username.lower()), (0, 0.0))
    if until > time.time():
        raise HTTPException(status_code=429, detail=f"로그인 시도가 너무 많습니다. {int(until - time.time())}초 뒤에 다시 시도하세요.")


def throttle_fail(ip: str, username: str) -> None:
    key = (ip, username.lower())
    n, _ = _fails.get(key, (0, 0.0))
    n += 1
    _fails[key] = (n, time.time() + LOCK_SECONDS if n >= MAX_FAILS else 0.0)


def throttle_ok(ip: str, username: str) -> None:
    _fails.pop((ip, username.lower()), None)


def throttle_reset() -> None:  # 테스트용
    _fails.clear()


# ───────────────────────── 세션 ─────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite는 tz를 잃는다


def client_ip(request: Request) -> str:
    # Cloudflare 터널 뒤에서는 실제 접속자 IP가 이 헤더에 있다
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def create_session(db: Session, user: User, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(id=token, user_id=user.id, expires_at=_utcnow() + timedelta(hours=SESSION_HOURS),
                       ip=client_ip(request), user_agent=request.headers.get("user-agent", "")[:300]))
    user.last_login_at = _utcnow()
    db.commit()
    return token


def revoke_sessions(db: Session, user_id: int, except_token: str | None = None) -> None:
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user_id)).all():
        if s.id != except_token:
            db.delete(s)


def is_https(request: Request) -> bool:
    """TLS는 프록시(nginx/frps, Cloudflare)가 종단한다. 프록시가 X-Forwarded-Proto를 안 줘도
    공개 도메인(MARKERLESS_SECURE_HOSTS, 기본 wizbase.ai.kr)으로 들어오면 https로 본다."""
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        return True
    host = (request.headers.get("host") or "").split(":")[0].lower()
    suffixes = [x.strip().lower() for x in os.environ.get("MARKERLESS_SECURE_HOSTS", "wizbase.ai.kr").split(",") if x.strip()]
    return any(host == x or host.endswith("." + x) for x in suffixes)


# ───────────────────────── 의존성 ─────────────────────────

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """로그인 사용자 (없으면 401). 변경 요청은 CSRF 헤더도 확인한다."""
    token = request.cookies.get(SESSION_COOKIE)
    sess = db.get(AuthSession, token) if token else None
    if sess is None or _aware(sess.expires_at) < _utcnow():
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    user = db.get(User, sess.user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="비활성화된 계정입니다")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="잘못된 요청입니다 (CSRF)")
    request.state.session_token = token
    if user.must_change_password and enforce_pw_change() and not request.url.path.endswith(PW_EXEMPT):
        raise HTTPException(status_code=403, detail="PASSWORD_CHANGE_REQUIRED: 기본/임시 비밀번호입니다. 새 비밀번호로 변경해야 계속 사용할 수 있습니다")
    return user


def require_role(minimum: str):
    """minimum 이상의 역할만 통과."""
    need = ROLE_RANK[minimum]

    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, 0) < need:
            raise HTTPException(status_code=403, detail=f"권한이 없습니다 ({ROLE_LABEL[minimum]} 이상 필요)")
        return user

    return dep


def audit(db: Session, actor: str, action: str, target_type: str = "", target_id: str = "",
          detail: dict | None = None, ip: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, target_type=target_type, target_id=str(target_id),
                    detail=detail or {}, ip=ip))
