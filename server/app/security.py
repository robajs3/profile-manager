import time
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .models import User, get_db, utcnow

_ph = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


def hash_password(p: str) -> str:
    return _ph.hash(p)


def verify_password(h: str, p: str) -> bool:
    try:
        return _ph.verify(h, p)
    except Exception:
        return False


def make_token(user: User) -> str:
    now = utcnow()
    return jwt.encode(
        {"sub": str(user.id), "iat": now, "exp": now + timedelta(days=settings.token_days)},
        settings.secret_key, algorithm="HS256")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    if not creds:
        raise HTTPException(401, "not_authenticated")
    try:
        data = jwt.decode(creds.credentials, settings.secret_key, algorithms=["HS256"])
        user = db.get(User, int(data["sub"]))
    except Exception:
        raise HTTPException(401, "invalid_token")
    if not user or not user.is_active:
        raise HTTPException(401, "invalid_token")
    return user


# --- prosty limiter prób logowania (w pamięci; wystarczy dla jednego kontenera) ---
_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW = 5, 300


def rate_check(key: str):
    now = time.time()
    hits = [t for t in _fails.get(key, []) if now - t < WINDOW]
    _fails[key] = hits
    if len(hits) >= MAX_FAILS:
        raise HTTPException(429, "too_many_attempts")


def rate_fail(key: str):
    _fails.setdefault(key, []).append(time.time())


def rate_reset(key: str):
    _fails.pop(key, None)
