"""Ścieżki, ustawienia i lokalny magazyn profili (wszystko na dysku klienta)."""
import base64
import gzip
import json
import os
import platform
import random
import re
import shutil
import threading
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "ProfileManager"
PROFILES = ROOT / "profiles"
ADDONS_CHROME = ROOT / "addons_chrome"   # rozszerzenia Chrome (CRX rozpakowane)
LOGS = ROOT / "logs"
TMP = ROOT / "tmp"
SETTINGS_FILE = ROOT / "settings.json"
for _d in (PROFILES, ADDONS_CHROME, LOGS, TMP):
    _d.mkdir(parents=True, exist_ok=True)

ENGINES = {"chrome": "Chrome (fingerprint-chromium)"}   # Firefox (Camoufox) został usunięty z aplikacji

DEFAULT_CONFIG = {
    "engine": "chrome", "fp_seed": 0, "locale": "", "timezone": "", "start_url": "about:blank",
    "block_webrtc": False, "addons": [], "bookmarks": [], "sync": True, "notes": "",
}

_SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://|(?:about|chrome|chrome-extension|file|data|view-source|javascript):)", re.I)

_LOCAL = re.compile(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|\[[0-9a-f:]+\])(?=[:/?#]|$)", re.I)   # jak omnibox Chrome: lokalne adresy po http


def normalize_url(text: str) -> str:
    """Dopisuje https://, jeśli adres nie ma schematu (np. `example.com` -> `https://example.com`).
    Playwright odrzuca adresy bez schematu błędem „Cannot navigate to invalid URL”."""
    text = (text or "").strip()
    if not text or text == "about:blank":
        return text
    if _SCHEME.match(text):
        return text
    text = text.lstrip("/")
    return ("http://" if _LOCAL.match(text) else "https://") + text


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


# ---------- fingerprint (serwer wymaga pola; dla Chrome to tylko znacznik, właściwy seed jest w configu) ----------
def encode_fp(fp: dict) -> str:
    return base64.b64encode(gzip.compress(json.dumps(fp).encode(), 6)).decode()


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


_meta_lock = threading.RLock()


def save_meta(meta: dict):
    with _meta_lock:
        pdir(meta["id"]).mkdir(parents=True, exist_ok=True)
        f = pdir(meta["id"]) / "meta.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, f)


def update_meta(pid: str, **kw) -> dict | None:
    """Atomowy odczyt-zmiana-zapis (kilka wątków może dotykać meta.json jednocześnie)."""
    with _meta_lock:
        m = load_meta(pid)
        if m is None:
            return None
        m.update(kw)
        save_meta(m)
        return m


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
    cfg["engine"] = "chrome"
    cfg["fp_seed"] = cfg.get("fp_seed") or random.randint(100000, 2**31 - 1)   # seed leży w configu, więc synchronizuje się z serwerem
    write_fp_encoded(pid, encode_fp({"engine": "chrome"}))
    meta = {"id": pid, "name": name, "config": cfg, "revision": 0, "dirty": False}
    save_meta(meta)
    return meta


def merged_config(meta: dict) -> dict:
    return {**DEFAULT_CONFIG, **meta.get("config", {})}


def delete_local(pid: str):
    shutil.rmtree(pdir(pid), ignore_errors=True)
