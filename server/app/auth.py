import hashlib
import secrets
from datetime import timedelta

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Invite, User, get_db, utcnow
from .security import (current_user, hash_password, make_token, rate_check,
                       rate_fail, rate_reset, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])


class Creds(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=256)


class RegisterBody(Creds):
    invite_code: str | None = None


class LoginBody(Creds):
    totp_code: str | None = None


def _h(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


def _check_second_factor(user: User, code: str) -> bool:
    code = code.strip().replace(" ", "")
    if user.totp_secret and pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
        return True
    hashed = _h(code)
    if hashed in (user.backup_codes or []):
        user.backup_codes = [c for c in user.backup_codes if c != hashed]  # kod jednorazowy
        return True
    return False


@router.post("/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    mode = settings.registration_mode
    if mode == "closed":
        raise HTTPException(403, "registration_closed")
    if settings.max_users and db.scalar(select(func.count(User.id))) >= settings.max_users:
        raise HTTPException(403, "user_limit_reached")
    invite = None
    if mode == "invite":
        invite = db.get(Invite, (body.invite_code or "").strip())
        if (not invite or invite.used_by is not None
                or (invite.expires_at and invite.expires_at < utcnow())):
            raise HTTPException(403, "invalid_invite")
    username = body.username.lower()
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, "username_taken")
    first = db.scalar(select(func.count(User.id))) == 0
    user = User(username=username, password_hash=hash_password(body.password), is_admin=first)
    db.add(user)
    db.flush()
    if invite:
        invite.used_by = user.id
    db.commit()
    return {"token": make_token(user), "username": user.username, "totp_enabled": False}


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else '?'}|{body.username.lower()}"
    rate_check(key)
    user = db.scalar(select(User).where(User.username == body.username.lower()))
    if not user or not user.is_active or not verify_password(user.password_hash, body.password):
        rate_fail(key)
        raise HTTPException(401, "invalid_credentials")
    if user.totp_enabled:
        if not body.totp_code:
            raise HTTPException(401, "totp_required")
        if not _check_second_factor(user, body.totp_code):
            rate_fail(key)
            raise HTTPException(401, "invalid_totp")
        db.commit()
    rate_reset(key)
    return {"token": make_token(user), "username": user.username, "totp_enabled": user.totp_enabled}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"username": user.username, "is_admin": user.is_admin, "totp_enabled": user.totp_enabled,
            "backup_codes_left": len(user.backup_codes or [])}


@router.post("/totp/setup")
def totp_setup(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.totp_enabled:
        raise HTTPException(400, "totp_already_enabled")
    user.totp_secret = pyotp.random_base32()
    db.commit()
    uri = pyotp.TOTP(user.totp_secret).provisioning_uri(user.username, issuer_name=settings.server_name)
    return {"secret": user.totp_secret, "uri": uri}


class CodeBody(BaseModel):
    code: str


@router.post("/totp/enable")
def totp_enable(body: CodeBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.totp_enabled or not user.totp_secret:
        raise HTTPException(400, "totp_not_ready")
    if not pyotp.TOTP(user.totp_secret).verify(body.code.strip(), valid_window=1):
        raise HTTPException(400, "invalid_totp")
    codes = [secrets.token_hex(4) for _ in range(8)]
    user.backup_codes = [_h(c) for c in codes]
    user.totp_enabled = True
    db.commit()
    return {"backup_codes": codes}  # pokazywane tylko raz


class DisableBody(BaseModel):
    password: str
    code: str


@router.post("/totp/disable")
def totp_disable(body: DisableBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.totp_enabled:
        return {"ok": True}
    if not verify_password(user.password_hash, body.password) or not _check_second_factor(user, body.code):
        raise HTTPException(400, "invalid_credentials")
    user.totp_enabled, user.totp_secret, user.backup_codes = False, None, []
    db.commit()
    return {"ok": True}


class InviteBody(BaseModel):
    days: int = 7


@router.post("/invite")
def create_invite(body: InviteBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.is_admin:
        raise HTTPException(403, "admin_only")
    inv = Invite(code=secrets.token_urlsafe(12), expires_at=utcnow() + timedelta(days=body.days))
    db.add(inv)
    db.commit()
    return {"code": inv.code, "expires_at": inv.expires_at}
