"""Silnik Chrome: fingerprint-chromium (otwartoźródłowy Chromium z maskowaniem fingerprintu w binarce, BSD-3).
Binarka jest pobierana z GitHub Releases przy pierwszym użyciu. Brak kluczy i brak limitu jednoczesnych sesji."""
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import requests

from . import core

REPO = "adryfish/fingerprint-chromium"
ENGINE_DIR = core.ROOT / "engines" / "fp-chromium"
READY = ENGINE_DIR / ".ready.json"
EXE_NAME = "chrome.exe" if sys.platform.startswith("win") else "chrome"


def exe_path() -> Path | None:
    env = os.environ.get("PM_CHROMIUM_EXE")          # ręczne wskazanie własnej binarki
    if env and Path(env).exists():
        return Path(env)
    try:
        j = json.loads(READY.read_text("utf-8"))
        p = ENGINE_DIR / j["exe"]
        return p if p.exists() else None
    except Exception:
        return None


def version() -> str | None:
    try:
        return json.loads(READY.read_text("utf-8"))["tag"]
    except Exception:
        return None


def _latest() -> tuple[str, list[tuple[str, str]]]:
    try:   # API bywa limitowane (60 zapytań/h na IP), więc jest wariant zapasowy
        r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=20,
                         headers={"Accept": "application/vnd.github+json"})
        if r.ok:
            j = r.json()
            return j["tag_name"], [(a["name"], a["browser_download_url"]) for a in j["assets"]]
    except requests.RequestException:
        pass
    r = requests.get(f"https://github.com/{REPO}/releases/latest", timeout=20)
    r.raise_for_status()
    tag = r.url.rstrip("/").split("/")[-1]
    h = requests.get(f"https://github.com/{REPO}/releases/expanded_assets/{tag}", timeout=20)
    h.raise_for_status()
    links = re.findall(r'href="(/%s/releases/download/[^"]+)"' % re.escape(REPO), h.text)
    return tag, [(l.split("/")[-1], "https://github.com" + l) for l in links]


def pick_asset(assets: list[tuple[str, str]]) -> tuple[str, str]:
    if sys.platform.startswith("win"):
        suffix = "windows_x64.zip"
    elif sys.platform.startswith("linux"):
        suffix = "linux.tar.xz"
    else:
        raise RuntimeError("Silnik Chrome jest wspierany w aplikacji na Windows i Linuksie")
    for name, url in assets:
        if name.endswith(suffix):
            return name, url
    raise RuntimeError(f"W najnowszym wydaniu nie ma pliku *{suffix}")


def download_latest(progress=lambda t, p=-1: None) -> str:
    progress("Sprawdzam najnowsze wydanie fingerprint-chromium…", -1)
    tag, assets = _latest()
    name, url = pick_asset(assets)
    arc = core.TMP / name
    with requests.get(url, stream=True, timeout=(10, 120)) as dl:
        dl.raise_for_status()
        total, done = int(dl.headers.get("Content-Length", 0)), 0
        with open(arc, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                f.write(chunk); done += len(chunk)
                progress(f"Pobieram silnik Chrome {tag}: {done >> 20} MB", int(done * 100 / total) if total else -1)
    progress("Rozpakowuję silnik Chrome…", -1)
    dest = ENGINE_DIR / tag
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(arc) as z:
            z.extractall(dest)
    else:
        with tarfile.open(arc, "r:xz") as t:
            t.extractall(dest, filter="tar")
    arc.unlink(missing_ok=True)
    exes = sorted(dest.rglob(EXE_NAME), key=lambda p: len(p.parts))
    if not exes:
        raise RuntimeError(f"W paczce nie znaleziono {EXE_NAME}")
    READY.write_text(json.dumps({"tag": tag, "exe": str(exes[0].relative_to(ENGINE_DIR))}), "utf-8")
    for old in ENGINE_DIR.iterdir():          # zostaw tylko aktualną wersję
        if old.is_dir() and old.name != tag:
            shutil.rmtree(old, ignore_errors=True)
    return tag


def ensure(progress=lambda t, p=-1: None) -> Path:
    p = exe_path()
    if p:
        return p
    download_latest(progress)
    return exe_path()
