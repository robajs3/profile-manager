import hashlib
import re
import shutil
import uuid
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Profile, User, get_db, utcnow
from .security import current_user
from .storage import TMP, blob_dir, blob_path, decrypt_iter, encrypt_file, prune

router = APIRouter(prefix="/profiles", tags=["profiles"])
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_BYTES = settings.max_profile_mb * 1024 * 1024


class DeviceBody(BaseModel):
    device_id: str = Field(min_length=8, max_length=64)
    device_name: str = Field(default="", max_length=128)


class ProfileCreate(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    config: dict = {}
    fingerprint: str | None = None


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    config: dict | None = None
    fingerprint: str | None = None


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lock_active(p: Profile) -> bool:
    exp = _aware(p.lock_expires_at)
    return bool(p.lock_device_id and exp and exp > utcnow())


def ser(p: Profile) -> dict:
    return {
        "id": p.id, "name": p.name, "config": p.config, "revision": p.revision,
        "blob_size": p.blob_size, "blob_sha256": p.blob_sha256, "updated_at": p.updated_at,
        "lock": {"active": lock_active(p), "device_id": p.lock_device_id,
                 "device_name": p.lock_device_name, "expires_at": p.lock_expires_at}
        if lock_active(p) else {"active": False},
    }


def own(db: Session, user: User, pid: str) -> Profile:
    p = db.get(Profile, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "profile_not_found")
    return p


def require_holder(p: Profile, device_id: str):
    if not lock_active(p) or p.lock_device_id != device_id:
        raise HTTPException(409, {"code": "lock_required"})


@router.get("")
def list_profiles(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Profile).where(Profile.user_id == user.id).order_by(Profile.name)).all()
    return [ser(p) for p in rows]


@router.post("")
def create_profile(body: ProfileCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not UUID_RE.match(body.id):
        raise HTTPException(400, "bad_id")
    if db.get(Profile, body.id):
        raise HTTPException(409, "profile_exists")
    if settings.max_profiles_per_user:
        n = db.scalar(select(func.count(Profile.id)).where(Profile.user_id == user.id))
        if n >= settings.max_profiles_per_user:
            raise HTTPException(403, "profile_limit_reached")
    p = Profile(id=body.id, user_id=user.id, name=body.name, config=body.config, fingerprint=body.fingerprint)
    db.add(p)
    db.commit()
    return ser(p)


@router.put("/{pid}")
def update_profile(pid: str, body: ProfileUpdate, x_device_id: str = Header(default=""),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own(db, user, pid)
    if lock_active(p) and p.lock_device_id != x_device_id:
        raise HTTPException(409, {"code": "locked", "device_name": p.lock_device_name})
    if body.name is not None:
        p.name = body.name
    if body.config is not None:
        p.config = body.config
    if body.fingerprint is not None:
        p.fingerprint = body.fingerprint
    p.updated_at = utcnow()
    db.commit()
    return ser(p)


@router.get("/{pid}/fingerprint")
def get_fingerprint(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"fingerprint": own(db, user, pid).fingerprint}


@router.delete("/{pid}")
def delete_profile(pid: str, x_device_id: str = Header(default=""),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own(db, user, pid)
    if lock_active(p) and p.lock_device_id != x_device_id:
        raise HTTPException(409, {"code": "locked", "device_name": p.lock_device_name})
    db.delete(p)
    db.commit()
    shutil.rmtree(blob_dir(user.id, pid), ignore_errors=True)
    return {"ok": True}


# ---------------- blokada (lease) ----------------
@router.post("/{pid}/lock")
def lock(pid: str, body: DeviceBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.scalar(select(Profile).where(Profile.id == pid, Profile.user_id == user.id).with_for_update())
    if not p:
        raise HTTPException(404, "profile_not_found")
    if lock_active(p) and p.lock_device_id != body.device_id:
        raise HTTPException(409, {"code": "locked", "device_name": p.lock_device_name,
                                  "expires_at": _aware(p.lock_expires_at).isoformat()})
    p.lock_device_id, p.lock_device_name = body.device_id, body.device_name
    p.lock_expires_at = utcnow() + timedelta(seconds=settings.lease_ttl_seconds)
    db.commit()
    return ser(p)


@router.post("/{pid}/heartbeat")
def heartbeat(pid: str, body: DeviceBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own(db, user, pid)
    if p.lock_device_id != body.device_id:
        raise HTTPException(409, {"code": "lock_lost"})
    # wygasłą własną blokadę można odnowić, o ile nikt jej nie przejął
    p.lock_expires_at = utcnow() + timedelta(seconds=settings.lease_ttl_seconds)
    db.commit()
    return {"ok": True}


@router.post("/{pid}/unlock")
def unlock(pid: str, body: DeviceBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own(db, user, pid)
    if p.lock_device_id == body.device_id:
        p.lock_device_id = p.lock_device_name = p.lock_expires_at = None
        db.commit()
    return {"ok": True}


# ---------------- dane profilu ----------------
@router.get("/{pid}/data")
def download(pid: str, x_device_id: str = Header(), user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    p = own(db, user, pid)
    require_holder(p, x_device_id)
    path = blob_path(user.id, pid, p.revision)
    if p.revision == 0 or not path.exists():
        raise HTTPException(404, "no_data")
    return StreamingResponse(decrypt_iter(path), media_type="application/octet-stream",
                             headers={"X-Revision": str(p.revision), "X-SHA256": p.blob_sha256 or "",
                                      "Content-Length": str(p.blob_size)})


@router.put("/{pid}/data")
async def upload(pid: str, request: Request, x_device_id: str = Header(), x_base_revision: int = Header(),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = own(db, user, pid)
    require_holder(p, x_device_id)
    if x_base_revision != p.revision:
        raise HTTPException(409, {"code": "revision_conflict", "server_revision": p.revision})
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_BYTES:
        raise HTTPException(413, "profile_too_large")
    if settings.user_quota_mb:
        used = db.scalar(select(func.coalesce(func.sum(Profile.blob_size), 0))
                         .where(Profile.user_id == user.id, Profile.id != pid))
        if used + int(cl or 0) > settings.user_quota_mb * 1024 * 1024:
            raise HTTPException(413, "quota_exceeded")

    TMP.mkdir(parents=True, exist_ok=True)
    tmp = TMP / f"{uuid.uuid4().hex}.part"
    sha, size = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "profile_too_large")
                sha.update(chunk)
                f.write(chunk)
        db.refresh(p)  # upload mógł trwać długo: sprawdź stan jeszcze raz
        require_holder(p, x_device_id)
        if x_base_revision != p.revision:
            raise HTTPException(409, {"code": "revision_conflict", "server_revision": p.revision})
        new_rev = p.revision + 1
        await run_in_threadpool(encrypt_file, tmp, blob_path(user.id, pid, new_rev))
    finally:
        tmp.unlink(missing_ok=True)

    p.revision, p.blob_size, p.blob_sha256, p.updated_at = new_rev, size, sha.hexdigest(), utcnow()
    p.lock_expires_at = utcnow() + timedelta(seconds=settings.lease_ttl_seconds)
    db.commit()
    prune(user.id, pid, new_rev, settings.keep_backups)
    return {"revision": new_rev, "sha256": p.blob_sha256}
