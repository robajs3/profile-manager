"""Wstępnie dodawane zakładki: wpisujemy je do pliku `Default/Bookmarks` profilu Chrome tuż przed startem przeglądarki.

Zasady:
- zakładki z configu profilu trafiają na pasek zakładek, ale tylko raz (lista już dodanych adresów leży w profilu,
  w `pm-bookmarks.json`, więc zakładka usunięta przez użytkownika nie wraca, a nowo dopisana w configu doda się przy
  następnym uruchomieniu; plik synchronizuje się razem z profilem);
- istniejące zakładki użytkownika nie są ruszane, duplikaty (ten sam adres) są pomijane.
"""
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from . import core

APPLIED_FILE = "pm-bookmarks.json"
# stałe GUID-y katalogów głównych, takie same jak w Chrome
ROOT_DEFS = (("bookmark_bar", "1", "Bookmarks bar", "0bc5d13f-2cba-5d74-951f-3f233fe6c908"),
             ("other", "2", "Other bookmarks", "82b081ec-3dd3-529c-8475-ab6c344590dd"),
             ("synced", "3", "Mobile bookmarks", "4cf2e351-0e85-532b-bb37-df045d8f8d0f"))


def parse_text(text: str) -> list[dict]:
    """Jedna zakładka na linię: `Tytuł | adres` albo samo `adres`. Puste linie i linie z # na początku są pomijane."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        title, sep, url = line.rpartition("|")
        url = core.normalize_url(url if sep else line)
        title = title.strip() if sep else ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": title or _default_title(url), "url": url})
    return out


def to_text(items: list[dict]) -> str:
    return "\n".join(f"{b['title']} | {b['url']}" if b.get("title") else b["url"] for b in items or [])


def _default_title(url: str) -> str:
    host = urlsplit(url).netloc
    return host[4:] if host.startswith("www.") else (host or url)


def _spec(url: str) -> str:
    """Przybliżenie kanonicznej postaci adresu (GURL::spec): małe litery w schemacie/hoście, `/` dla pustej ścieżki."""
    try:
        p = urlsplit(url)
        if p.scheme in ("http", "https") and p.netloc:
            netloc = p.netloc.lower()
            if netloc.endswith(":80" if p.scheme == "http" else ":443"):     # domyślny port nie jest częścią adresu
                netloc = netloc.rsplit(":", 1)[0]
            return urlunsplit((p.scheme, netloc, quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~"), p.query, p.fragment))
    except ValueError:
        pass
    return url


def _chrome_time() -> str:
    """Czas w formacie Chrome: mikrosekundy od 1601-01-01."""
    return str(int((time.time() + 11644473600) * 1_000_000))


def _empty() -> dict:
    now = _chrome_time()
    return {"roots": {key: {"children": [], "date_added": now, "date_last_used": "0", "date_modified": now,
                            "guid": guid, "id": id_, "name": name, "type": "folder"}
                      for key, id_, name, guid in ROOT_DEFS},
            "version": 1}


def _walk(node: dict):
    yield node
    for c in node.get("children", []):
        yield from _walk(c)


def _checksum(roots: dict) -> str:
    """Suma kontrolna jak w Chrome (BookmarkCodec): MD5 po id, tytule (UTF-16LE), typie i adresie, w kolejności drzewa."""
    md5 = hashlib.md5()

    def upd(node):
        md5.update(str(node["id"]).encode())
        md5.update(node.get("name", "").encode("utf-16-le"))
        if node.get("type") == "url":
            md5.update(b"url")
            md5.update(node.get("url", "").encode())
        else:
            md5.update(b"folder")
            for c in node.get("children", []):
                upd(c)

    for key, *_ in ROOT_DEFS:
        if key in roots:
            upd(roots[key])
    return md5.hexdigest()


def _read_json(f: Path, default):
    try:
        return json.loads(f.read_text("utf-8"))
    except Exception:
        return default


def _write_json(f: Path, obj):
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f.suffix + ".pm-tmp")
    tmp.write_text(json.dumps(obj, indent=3, ensure_ascii=False), "utf-8")
    tmp.replace(f)


def seed(data: Path, items: list[dict]) -> int:
    """Dodaje do profilu (katalog danych Chrome) zakładki, których jeszcze nie dodano. Zwraca liczbę dodanych."""
    if not items:
        return 0
    applied_f = data / APPLIED_FILE
    applied = set(_read_json(applied_f, {}).get("applied", []))
    todo = [b for b in items if b["url"] not in applied]
    if not todo:
        return 0

    bm_file = data / "Default" / "Bookmarks"
    doc = _read_json(bm_file, None)
    if not isinstance(doc, dict) or "roots" not in doc:
        doc = _empty()
    roots = doc["roots"]
    bar = roots.setdefault("bookmark_bar", _empty()["roots"]["bookmark_bar"])
    bar.setdefault("children", [])

    existing = {_spec(n["url"]) for r in roots.values() if isinstance(r, dict) for n in _walk(r) if n.get("type") == "url"}
    next_id = max([int(n["id"]) for r in roots.values() if isinstance(r, dict) for n in _walk(r)
                   if str(n.get("id", "")).isdigit()] + [3]) + 1
    now, added = _chrome_time(), 0
    for b in todo:
        applied.add(b["url"])
        spec = _spec(b["url"])
        if spec in existing:
            continue
        bar["children"].append({"date_added": now, "date_last_used": "0", "guid": str(uuid.uuid4()),
                                "id": str(next_id), "name": b["title"], "type": "url", "url": spec})
        existing.add(spec)
        next_id += 1
        added += 1
    if added:
        bar["date_modified"] = now
        doc["checksum"] = _checksum(roots)
        _write_json(bm_file, doc)
        _show_bar_once(data)
    _write_json(applied_f, {"applied": sorted(applied)})
    return added


def _show_bar_once(data: Path):
    """Pasek zakładek jest domyślnie ukryty poza kartą „Nowa karta”, więc po pierwszym dodaniu zakładek go włączamy
    (tylko jeśli użytkownik nie ustawił tego sam)."""
    f = data / "Default" / "Preferences"
    prefs = _read_json(f, {}) if f.exists() else {}
    if not isinstance(prefs, dict):
        return
    bar = prefs.setdefault("bookmark_bar", {})
    if "show_on_all_tabs" not in bar:
        bar["show_on_all_tabs"] = True
        _write_json(f, prefs)
