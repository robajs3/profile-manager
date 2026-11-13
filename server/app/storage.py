"""Szyfrowanie blobów po stronie serwera: strumieniowo, kawałkami po 4 MB (Fernet)."""
import os
import struct
from pathlib import Path

from cryptography.fernet import Fernet

from .config import settings

CHUNK = 4 * 1024 * 1024
DATA = Path(settings.data_dir)
TMP = DATA / "tmp"
BLOBS = DATA / "blobs"


def _fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def check_key():
    _fernet()  # rzuci wyjątek, jeśli klucz jest zły


def blob_dir(user_id: int, pid: str) -> Path:
    return BLOBS / str(user_id) / pid


def blob_path(user_id: int, pid: str, rev: int) -> Path:
    return blob_dir(user_id, pid) / f"{rev}.bin"


def encrypt_file(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    f = _fernet()
    with open(src, "rb") as i, open(tmp, "wb") as o:
        while chunk := i.read(CHUNK):
            tok = f.encrypt(chunk)
            o.write(struct.pack(">I", len(tok)))
            o.write(tok)
    os.replace(tmp, dst)


def decrypt_iter(path: Path):
    f = _fernet()
    with open(path, "rb") as i:
        while hdr := i.read(4):
            (n,) = struct.unpack(">I", hdr)
            yield f.decrypt(i.read(n))


def prune(user_id: int, pid: str, current_rev: int, keep: int):
    d = blob_dir(user_id, pid)
    if not d.exists():
        return
    for p in d.glob("*.bin"):
        try:
            if int(p.stem) < current_rev - keep:
                p.unlink()
        except ValueError:
            pass
