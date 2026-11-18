"""Biblioteka rozszerzeń: Firefox (addons.mozilla.org) i Chrome (Chrome Web Store, CRX)."""
import base64
import hashlib
import io
import json
import re
import shutil
import zipfile

import requests

from . import core

AMO_API = "https://addons.mozilla.org/api/v5/addons/addon/{}/?lang=en-US"
CWS_CRX = ("https://clients2.google.com/service/update2/crx?response=redirect&os=win&arch=x64&os_arch=x86_64"
           "&nacl_arch=x86-64&prod=chromiumcrx&prodchannel=&prodversion=150.0.0.0&lang=en-US"
           "&acceptformat=crx2,crx3&x=id%3D{}%26installsource%3Dondemand%26uc")

PRESETS = {
    "firefox": {"Browsec": "browsec", "Steam Inventory Helper": "steam-inventory-helper"},
    "chrome": {"Browsec": "omghfjlpggmjjaagoclmmobgdodcjboh",
               "Steam Inventory Helper": "cmeakgjggjdlcpncigglobpjbkabhmjl"},
}


def _root(engine: str):
    return core.ADDONS_CHROME if engine == "chrome" else core.ADDONS


def parse_ref(text: str, engine: str = "firefox") -> str:
    text = text.strip()
    if engine == "chrome":
        m = re.search(r"[a-p]{32}", text)
        if not m:
            raise RuntimeError("Podaj 32-znakowe ID rozszerzenia albo link z Chrome Web Store")
        return m.group(0)
    m = re.search(r"/addon/([^/?#]+)", text)
    return (m.group(1) if m else text).strip("/")


def path(slug: str, engine: str = "firefox"):
    return _root(engine) / slug


def installed(engine: str = "firefox") -> list[dict]:
    out = []
    for d in sorted(_root(engine).iterdir()):
        f = d / ".pm.json"
        if f.exists():
            try:
                out.append({"slug": d.name, **json.loads(f.read_text("utf-8"))})
            except Exception:
                pass
    return out


def _swap_in(engine, slug, staging, name, ver):
    (staging / ".pm.json").write_text(json.dumps({"name": name, "version": ver}), "utf-8")
    target = path(slug, engine)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


# ------------------------------------------------------------------ Firefox
def install_firefox(slug: str, progress) -> dict:
    progress(f"Szukam {slug} na AMO…", -1)
    r = requests.get(AMO_API.format(slug), timeout=20)
    if r.status_code == 404:
        raise RuntimeError(f"Nie znaleziono dodatku „{slug}” na addons.mozilla.org")
    r.raise_for_status()
    j = r.json()
    ver, url = j["current_version"]["version"], j["current_version"]["file"]["url"]
    name = j["name"] if isinstance(j["name"], str) else next(iter(j["name"].values()))
    tmp = core.TMP / f"{slug}.xpi"
    with requests.get(url, stream=True, timeout=(10, 120)) as dl:
        dl.raise_for_status()
        total, done = int(dl.headers.get("Content-Length", 0)), 0
        with open(tmp, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                f.write(chunk); done += len(chunk)
                progress(f"Pobieram {name} {ver}", int(done * 100 / total) if total else -1)
    staging = core.ADDONS / f"{slug}.new"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(staging)
    tmp.unlink(missing_ok=True)
    if not (staging / "manifest.json").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("Paczka nie zawiera manifest.json")
    _swap_in("firefox", slug, staging, name, ver)
    return {"slug": slug, "name": name, "version": ver}


# ------------------------------------------------------------------ Chrome (CRX)
def _varint(b: bytes, i: int):
    n = shift = 0
    while True:
        c = b[i]; i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _pb_fields(b: bytes):
    """Minimalny parser protobuf: zwraca listę (numer_pola, bajty) dla pól length-delimited."""
    i, out = 0, []
    while i < len(b):
        tag, i = _varint(b, i)
        wt = tag & 7
        if wt == 2:
            ln, i = _varint(b, i)
            out.append((tag >> 3, b[i:i + ln])); i += ln
        elif wt == 0:
            _, i = _varint(b, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            break
    return out


def crx_parse(data: bytes) -> tuple[bytes, bytes | None]:
    """Zwraca (zip, klucz_publiczny_DER). Klucz pozwala zachować stałe ID rozszerzenia na każdym komputerze."""
    if data[:2] == b"PK":
        return data, None
    if data[:4] != b"Cr24":
        raise RuntimeError("To nie jest plik CRX")
    ver = int.from_bytes(data[4:8], "little")
    if ver == 2:
        pk, sg = int.from_bytes(data[8:12], "little"), int.from_bytes(data[12:16], "little")
        return data[16 + pk + sg:], data[16:16 + pk]
    if ver == 3:
        hl = int.from_bytes(data[8:12], "little")
        fields = _pb_fields(data[12:12 + hl])
        crx_id = None
        for num, val in fields:
            if num == 10000:
                for n2, v2 in _pb_fields(val):
                    if n2 == 1:
                        crx_id = v2
        key = None
        for num, val in fields:
            if num in (2, 3):
                for n2, v2 in _pb_fields(val):
                    if n2 == 1 and (crx_id is None or hashlib.sha256(v2).digest()[:16] == crx_id):
                        key = v2
        return data[12 + hl:], key
    raise RuntimeError(f"Nieobsługiwana wersja CRX: {ver}")


def _resolve_name(root, manifest) -> str:
    name = manifest.get("name", "?")
    m = re.fullmatch(r"__MSG_(.+)__", name)
    if m:
        for loc in (manifest.get("default_locale"), "en", "en_US"):
            f = root / "_locales" / str(loc) / "messages.json"
            if f.exists():
                try:
                    msgs = {k.lower(): v for k, v in json.loads(f.read_text("utf-8-sig")).items()}
                    return msgs[m.group(1).lower()]["message"]
                except Exception:
                    pass
    return name


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"}
CWS_URLS = [CWS_CRX,
            "https://clients2.google.com/service/update2/crx?response=redirect&prodversion=150.0.0.0"
            "&acceptformat=crx2,crx3&x=id%3D{}%26uc"]


def ext_id_from_key(pub: bytes) -> str:
    """ID rozszerzenia Chrome = pierwsze 16 B sha256 klucza publicznego, zapisane literami a-p."""
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(pub).hexdigest()[:32])


def _fetch_crx(ext_id: str, progress) -> bytes:
    errors = []
    for url in CWS_URLS:
        try:
            progress(f"Pobieram {ext_id} z Chrome Web Store…", -1)
            with requests.get(url.format(ext_id), headers=UA, stream=True, timeout=(10, 120)) as dl:
                if dl.status_code != 200:
                    errors.append(f"HTTP {dl.status_code}")
                    continue
                buf, total = io.BytesIO(), int(dl.headers.get("Content-Length", 0))
                for chunk in dl.iter_content(1 << 20):
                    buf.write(chunk)
                    progress(f"Pobieram rozszerzenie… {buf.tell() >> 20} MB", int(buf.tell() * 100 / total) if total else -1)
                body = buf.getvalue()
                if body[:4] == b"Cr24" or body[:2] == b"PK":
                    return body
                errors.append(f"nieoczekiwana odpowiedź ({len(body)} B)")
        except requests.RequestException as e:
            errors.append(str(e)[:120])
    raise RuntimeError("Nie udało się pobrać rozszerzenia ze sklepu Chrome (" + "; ".join(errors) + "). "
                       "Użyj „Dodaj z pliku / folderu”: zainstaluj rozszerzenie w zwykłym Chrome i wskaż jego folder "
                       "z %LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\Extensions\\<id>\\<wersja>.")


def _finalize(engine: str, slug: str, staging, pub: bytes | None = None) -> dict:
    shutil.rmtree(staging / "_metadata", ignore_errors=True)   # dodaje go Chrome; Chromium odrzuca nazwy z „_” (poza _locales)
    mf = staging / "manifest.json"
    if not mf.exists():   # czasem manifest leży piętro niżej (zip z folderem)
        inner = [d for d in staging.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
        if len(inner) == 1:
            for item in inner[0].iterdir():
                shutil.move(str(item), staging / item.name)
        mf = staging / "manifest.json"
    if not mf.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("Nie znaleziono manifest.json (to nie jest rozpakowane rozszerzenie)")
    manifest = json.loads(mf.read_text("utf-8-sig"))
    if pub and engine == "chrome":   # stałe ID niezależnie od ścieżki, więc dane rozszerzenia przenoszą się między komputerami
        manifest["key"] = base64.b64encode(pub).decode()
        mf.write_text(json.dumps(manifest), "utf-8")
    name, ver = _resolve_name(staging, manifest), manifest.get("version", "?")
    _swap_in(engine, slug, staging, name, ver)
    return {"slug": slug, "name": name, "version": ver}


def install_chrome(ext_id: str, progress) -> dict:
    zbytes, pub = crx_parse(_fetch_crx(ext_id, progress))
    staging = core.ADDONS_CHROME / f"{ext_id}.new"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        z.extractall(staging)
    return _finalize("chrome", ext_id, staging, pub)


def install_from_file(path, engine: str = "chrome", progress=lambda t, p=-1: None) -> dict:
    """Instalacja z pliku .crx/.zip/.xpi albo z rozpakowanego folderu (niezależna od sklepu)."""
    from pathlib import Path
    p, pub = Path(path), None
    if p.is_dir():
        slug = p.parent.name if re.fullmatch(r"[a-p]{32}", p.parent.name) else "local-" + hashlib.sha1(p.name.encode()).hexdigest()[:10]
        staging = _root(engine) / f"{slug}.new"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(p, staging)
    else:
        data = p.read_bytes()
        zbytes, pub = crx_parse(data) if data[:4] == b"Cr24" or data[:2] == b"PK" else (None, None)
        if zbytes is None:
            raise RuntimeError("To nie jest plik .crx/.zip/.xpi")
        slug = ext_id_from_key(pub) if pub else "local-" + hashlib.sha1(p.stem.encode()).hexdigest()[:10]
        staging = _root(engine) / f"{slug}.new"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
            z.extractall(staging)
    progress("Instaluję rozszerzenie…", -1)
    return _finalize(engine, slug, staging, pub)


# ------------------------------------------------------------------ wspólne
def install(ref: str, engine: str = "firefox", progress=lambda t, p=-1: None) -> dict:
    ref = parse_ref(ref, engine)
    return install_chrome(ref, progress) if engine == "chrome" else install_firefox(ref, progress)


def remove(slug: str, engine: str = "firefox"):
    shutil.rmtree(path(slug, engine), ignore_errors=True)


def ensure(slugs: list[str], engine: str = "firefox", progress=lambda t, p=-1: None):
    """Doinstaluj brakujące (np. po przeniesieniu profilu na drugi komputer)."""
    for s in slugs:
        if not (path(s, engine) / "manifest.json").exists():
            install(s, engine, progress)
