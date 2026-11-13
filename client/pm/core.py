"""Ścieżki, ustawienia i lokalny magazyn profili (wszystko na dysku klienta)."""
import base64
import gzip
import json
import os
import platform
import shutil
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "ProfileManager"
PROFILES = ROOT / "profiles"
ADDONS = ROOT / "addons"
LOGS = ROOT / "logs"
TMP = ROOT / "tmp"
SETTINGS_FILE = ROOT / "settings.json"
for _d in (PROFILES, ADDONS, LOGS, TMP):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "os": "windows", "locale": "", "timezone": "", "start_url": "about:blank",
    "block_webrtc": False, "humanize": False, "ublock": False,
    "addons": [], "sync": True, "notes": "",
}


def load_settings() -> dict:
    s = {"server_url": "", "token": "", "username": "",
         "device_id": str(uuid.uuid4()), "device_name": platform.node() or "pc"}
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
    save_settings(s)
    return s


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")


# ---------- fingerprint (400 kB JSON -> gzip+base64, żeby tanio trzymać go na serwerze) ----------
def encode_fp(fp: dict) -> str:
    return base64.b64encode(gzip.compress(json.dumps(fp).encode(), 6)).decode()


def decode_fp(s: str) -> dict:
    return json.loads(gzip.decompress(base64.b64decode(s)))


def generate_fp(os_name: str) -> dict:
    from camoufox.fingerprints import generate_fingerprint
    return generate_fingerprint(os=os_name)


# ---------- profile lokalne ----------
def pdir(pid: str) -> Path:
    return PROFILES / pid


def data_dir(pid: str) -> Path:
    return pdir(pid) / "data"       # katalog profilu przeglądarki (cookies, storage, rozszerzenia)


def load_meta(pid: str) -> dict | None:
    f = pdir(pid) / "meta.json"
    try:
        return json.loads(f.read_text("utf-8"))
    except Exception:
        return None


def save_meta(meta: dict):
    pdir(meta["id"]).mkdir(parents=True, exist_ok=True)
    (pdir(meta["id"]) / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")


def list_local() -> list[dict]:
    out = []
    for d in PROFILES.iterdir():
        m = load_meta(d.name) if d.is_dir() else None
        if m:
            out.append(m)
    return sorted(out, key=lambda m: m["name"].lower())


def read_fp_encoded(pid: str) -> str | None:
    f = pdir(pid) / "fingerprint.txt"
    return f.read_text("utf-8") if f.exists() else None


def write_fp_encoded(pid: str, s: str):
    pdir(pid).mkdir(parents=True, exist_ok=True)
    (pdir(pid) / "fingerprint.txt").write_text(s, "utf-8")


def create_local(name: str, config: dict) -> dict:
    pid = str(uuid.uuid4())
    cfg = {**DEFAULT_CONFIG, **config}
    write_fp_encoded(pid, encode_fp(generate_fp(cfg["os"])))
    meta = {"id": pid, "name": name, "config": cfg, "revision": 0, "dirty": False}
    save_meta(meta)
    return meta


def merged_config(meta: dict) -> dict:
    return {**DEFAULT_CONFIG, **meta.get("config", {})}


def delete_local(pid: str):
    shutil.rmtree(pdir(pid), ignore_errors=True)
