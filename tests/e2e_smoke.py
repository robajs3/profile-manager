"""Test end-to-end bez Dockera i bez przeglądarki: serwer (SQLite) + dwa symulowane urządzenia.
Uruchom z katalogu głównego repo:  python tests/e2e_smoke.py"""
import os, shutil, sys, tempfile, threading, time, uuid
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(DATABASE_URL=f"sqlite:///{tmp}/pm.db", DATA_DIR=str(tmp / "srv"), REGISTRATION_MODE="open",
                  SECRET_KEY="test", APPDATA=str(tmp / "appdata"))
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root / "server"), str(root / "client")]

import pyotp, uvicorn
from app.main import app
srv = uvicorn.Server(uvicorn.Config(app, port=8765, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start(); time.sleep(2)

from pm import core, sync
from pm.api import Api, ApiError
base = "http://127.0.0.1:8765"
def device(c):
    s = {"server_url": base, "token": "", "device_id": c * 8, "device_name": "PC-" + c}
    return s, Api(s)
sA, A = device("a"); sB, B = device("b")
nop = lambda t, p=-1: None
def expect(label, code, fn):
    try: fn(); raise SystemExit(f"FAIL {label}: brak błędu")
    except ApiError as e:
        assert e.code == code, f"FAIL {label}: {e.code} != {code}"; print("ok  ", label, "->", e.code)

sA["token"] = A.register("tester", "supersecret1")["token"]
st = A.totp_setup(); codes = A.totp_enable(pyotp.TOTP(st["secret"]).now())["backup_codes"]
expect("login bez 2FA", "totp_required", lambda: B.login("tester", "supersecret1"))
expect("złe hasło", "invalid_credentials", lambda: B.login("tester", "zlehaslo123"))
sB["token"] = B.login("tester", "supersecret1", codes[0])["token"]; print("ok   login kodem zapasowym")
expect("kod zapasowy jednorazowy", "invalid_totp", lambda: B.login("tester", "supersecret1", codes[0]))
sB["token"] = B.login("tester", "supersecret1", pyotp.TOTP(st["secret"]).now())["token"]; print("ok   login TOTP")

pid = str(uuid.uuid4())
A.create_profile(pid, "acc1", {"sync": True}, "FP")
A.lock(pid)
expect("B nie zajmie profilu zajętego przez A", "locked", lambda: B.lock(pid))
data = core.data_dir(pid); data.mkdir(parents=True)
(data / "cookies.sqlite").write_bytes(os.urandom(300_000))
(data / "cache2").mkdir(); (data / "cache2" / "x").write_text("smiec")
core.save_meta({"id": pid, "name": "acc1", "config": {"sync": True}, "revision": 0, "dirty": True})
sync.finish(A, pid, nop)
assert core.load_meta(pid)["revision"] == 1; print("ok   A wysłał rewizję 1 i zwolnił blokadę")
blob = next((tmp / "srv" / "blobs").rglob("1.bin"))
assert blob.stat().st_size > 300_000 and b"cookies" not in blob.read_bytes(); print("ok   blob na serwerze jest zaszyfrowany")

shutil.move(str(core.pdir(pid)), str(tmp / "A_copy"))            # "komputer B" nie ma lokalnej kopii
core.save_meta({"id": pid, "name": "acc1", "config": {"sync": True}, "revision": 0, "dirty": False})
core.write_fp_encoded(pid, "FP")
sync.prepare(B, pid, nop)
d = core.data_dir(pid)
assert (d / "cookies.sqlite").read_bytes() == (tmp / "A_copy" / "data" / "cookies.sqlite").read_bytes()
assert not (d / "cache2").exists(); print("ok   B pobrał identyczne dane, cache pominięty")
expect("A zablokowany, gdy B używa", "locked", lambda: A.lock(pid))
(tmp / "x").write_bytes(b"1")
expect("upload bez blokady", "lock_required", lambda: A.upload(pid, tmp / "x", 1))
(d / "cookies.sqlite").write_bytes(b"nowe-cookies-z-B")
sync.finish(B, pid, nop); assert core.load_meta(pid)["revision"] == 2; print("ok   B wysłał rewizję 2")
A.lock(pid); expect("upload ze starej rewizji", "revision_conflict", lambda: A.upload(pid, tmp / "x", 1)); print("ok   A znów może zająć profil")
from datetime import datetime, timedelta, timezone
from app.models import Profile, SessionLocal
with SessionLocal() as db:
    pr = db.get(Profile, pid); pr.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=300); db.commit()
(tmp / "y").write_bytes(b"2")
assert A.upload(pid, tmp / "y", 2)["revision"] == 3; print("ok   upload po wygaśnięciu własnej blokady (nikt jej nie przejął)")
with SessionLocal() as db:
    pr = db.get(Profile, pid); pr.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=300); db.commit()
B.lock(pid)
expect("A po przejęciu blokady przez B", "lock_required", lambda: A.upload(pid, tmp / "y", 3))
print("\nWSZYSTKO OK")
os._exit(0)
