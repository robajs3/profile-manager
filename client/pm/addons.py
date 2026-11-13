"""Szybka instalacja rozszerzeń z addons.mozilla.org (AMO) do wspólnej biblioteki."""
import json
import re
import shutil
import zipfile

import requests

from . import core

AMO_API = "https://addons.mozilla.org/api/v5/addons/addon/{}/?lang=en-US"
PRESETS = {"Browsec": "browsec", "Steam Inventory Helper": "steam-inventory-helper"}


def parse_slug(text: str) -> str:
    text = text.strip()
    m = re.search(r"/addon/([^/?#]+)", text)
    return (m.group(1) if m else text).strip("/")


def path(slug: str):
    return core.ADDONS / slug


def installed() -> list[dict]:
    out = []
    for d in sorted(core.ADDONS.iterdir()):
        f = d / ".pm.json"
        if f.exists():
            try:
                out.append({"slug": d.name, **json.loads(f.read_text("utf-8"))})
            except Exception:
                pass
    return out


def install(slug: str, progress=lambda t, p=-1: None) -> dict:
    slug = parse_slug(slug)
    progress(f"Szukam {slug} na AMO…", -1)
    r = requests.get(AMO_API.format(slug), timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"Nie znaleziono dodatku „{slug}” na addons.mozilla.org")
    r.raise_for_status()
    j = r.json()
    ver, url = j["current_version"]["version"], j["current_version"]["file"]["url"]
    name = j["name"]
    name = name if isinstance(name, str) else next(iter(name.values()))
    tmp = core.TMP / f"{slug}.xpi"
    with requests.get(url, stream=True, timeout=(10, 120)) as dl:
        dl.raise_for_status()
        total, done = int(dl.headers.get("Content-Length", 0)), 0
        with open(tmp, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                f.write(chunk); done += len(chunk)
                progress(f"Pobieram {name} {ver}", int(done * 100 / total) if total else -1)
    target, staging = path(slug), core.ADDONS / f"{slug}.new"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(staging)
    tmp.unlink(missing_ok=True)
    if not (staging / "manifest.json").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("Paczka nie zawiera manifest.json")
    (staging / ".pm.json").write_text(json.dumps({"name": name, "version": ver}), "utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return {"slug": slug, "name": name, "version": ver}


def remove(slug: str):
    shutil.rmtree(path(slug), ignore_errors=True)


def ensure(slugs: list[str], progress=lambda t, p=-1: None):
    """Doinstaluj brakujące (np. po przeniesieniu profilu na drugi komputer)."""
    for s in slugs:
        if not (path(s) / "manifest.json").exists():
            install(s, progress)
