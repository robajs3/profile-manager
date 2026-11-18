"""Pakowanie/rozpakowywanie profilu oraz przepływ: lock -> pobranie -> praca -> wysyłka -> unlock."""
import shutil
import tarfile
import time
from pathlib import Path

from . import core

# cache'e i pliki tymczasowe nie są synchronizowane (cookies/storage/rozszerzenia - tak)
EXCLUDE_DIRS = {"Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "DawnCache", "DawnGraphiteCache",
                "DawnWebGPUCache", "Crashpad", "component_crx_cache", "extensions_crx_cache", "CacheStorage",
                "BrowserMetrics", "optimization_guide_model_store",
                "cache2", "startupCache", "thumbnails", "crashes", "minidumps", "shader-cache",
                "datareporting", "safebrowsing", "saved-telemetry-pings", "OfflineCache", "jumpListCache"}
EXCLUDE_FILES = {"lock", ".parentlock", "parent.lock", "SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"}


def pack(data: Path, out: Path):
    def flt(ti: tarfile.TarInfo):
        parts = Path(ti.name).parts
        if not parts:
            return ti
        if any(p in EXCLUDE_DIRS for p in parts) or parts[-1] in EXCLUDE_FILES:
            return None
        return ti
    with tarfile.open(out, "w:gz", compresslevel=3) as tar:
        tar.add(data, arcname=".", filter=flt)


def unpack(archive: Path, data: Path):
    new = data.with_name("data.new")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(new, filter="data")
    shutil.rmtree(data, ignore_errors=True)
    new.rename(data)


def _pct(progress, label):
    last = [-1]
    def cb(done, total):
        p = int(done * 100 / total) if total else 0
        if p != last[0]:
            last[0] = p
            progress(f"{label} {p}%", p)
    return cb


def prepare(api, pid: str, progress) -> str:
    """Zakłada blokadę i doprowadza lokalny katalog do najnowszej rewizji. Zwraca ostrzeżenie lub ''."""
    meta = core.load_meta(pid)
    cfg = core.merged_config(meta)
    warning = ""
    if cfg["sync"]:
        if not core.read_fp_encoded(pid):
            progress("Pobieram fingerprint…", -1)
            core.write_fp_encoded(pid, api.get_fingerprint(pid))
        progress("Zajmuję profil na serwerze…", -1)
        srv = api.lock(pid)
        try:
            if meta["revision"] != srv["revision"]:
                data = core.data_dir(pid)
                if meta.get("dirty") and data.exists():
                    data.rename(data.with_name(f"data.conflict-{int(time.time())}"))
                    warning = ("Lokalna kopia miała niezsynchronizowane zmiany, a na serwerze jest nowsza wersja. "
                               "Użyto wersji z serwera, lokalna została zachowana w katalogu data.conflict-*.")
                if srv["blob_size"] > 0:
                    arc = core.TMP / f"{pid}.dl"
                    api.download(pid, arc, _pct(progress, "Pobieranie profilu"))
                    progress("Rozpakowuję…", -1)
                    unpack(arc, data)
                    arc.unlink(missing_ok=True)
                else:
                    shutil.rmtree(data, ignore_errors=True)
                core.update_meta(pid, revision=srv["revision"])
        except Exception:
            try:
                api.unlock(pid)
            except Exception:
                pass
            raise
    core.update_meta(pid, dirty=True)
    core.data_dir(pid).mkdir(parents=True, exist_ok=True)
    return warning


def prepare_local(pid: str):
    """Ponowne wejście do profilu, który jest akurat wysyłany/czeka na wysyłkę: lokalny katalog jest
    najnowszy, blokada nadal należy do tego urządzenia, więc niczego nie pobieramy ani nie porównujemy."""
    core.update_meta(pid, dirty=True)
    core.data_dir(pid).mkdir(parents=True, exist_ok=True)


def snapshot(pid: str, progress) -> Path:
    """Zamraża stan profilu do archiwum (szybko). Po tym przeglądarkę można od razu uruchomić ponownie."""
    arc = core.TMP / f"{pid}.{int(time.time() * 1000)}.up"
    progress("Pakuję profil…", -1)
    pack(core.data_dir(pid), arc)
    return arc


def upload_snapshot(api, pid: str, arc: Path, progress):
    """Wysyła zamrożone archiwum. Bazową rewizję czyta w chwili wysyłki, więc kolejne migawki układają się w łańcuch."""
    meta = core.load_meta(pid)
    try:
        res = api.upload(pid, arc, meta["revision"], _pct(progress, "Wysyłanie profilu"))
        core.update_meta(pid, revision=res["revision"])
    finally:
        Path(arc).unlink(missing_ok=True)


def release(api, pid: str):
    """Koniec pracy z profilem: oznacz jako czysty i zwolnij blokadę na serwerze."""
    core.update_meta(pid, dirty=False)
    if core.merged_config(core.load_meta(pid))["sync"]:
        try:
            api.unlock(pid)
        except Exception:
            pass  # blokada i tak wygaśnie po TTL


def finish(api, pid: str, progress):
    """Wersja synchroniczna (testy): migawka + wysyłka + zwolnienie."""
    if not core.merged_config(core.load_meta(pid))["sync"]:
        core.update_meta(pid, dirty=False)
        return
    arc = snapshot(pid, progress)
    upload_snapshot(api, pid, arc, progress)
    release(api, pid)
