"""Proces przeglądarki: uruchamiany jako osobny proces (exe --worker <id>), żeby GUI się nie blokowało."""
import json
import os

from . import addons, bookmarks, core


def _cfg(pid):
    meta = core.load_meta(pid)
    return core.merged_config(meta)


def run(pid: str):
    cfg = _cfg(pid)
    if cfg["engine"] != "chrome":
        raise RuntimeError("Profile Firefox (Camoufox) nie są już obsługiwane. Utwórz nowy profil Chrome.")
    run_chrome(pid, cfg)


COOKIES_FILE = "pm-cookies.json"


def host_platform() -> str:
    import sys
    return "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux"


def chrome_args(pid: str, cfg: dict, paths: list[str]) -> list[str]:
    import hashlib
    seed = int(cfg.get("fp_seed") or int(hashlib.sha256(pid.encode()).hexdigest()[:7], 16) + 100000)
    args = [f"--fingerprint={seed}", f"--fingerprint-platform={host_platform()}", "--fingerprint-brand=Chrome",
            "--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
            "--disable-features=DisableLoadExtensionCommandLineSwitch"]
    if cfg["timezone"]:
        args.append(f"--timezone={cfg['timezone']}")
    if cfg["locale"]:
        args += [f"--lang={cfg['locale']}", f"--accept-lang={cfg['locale']},{cfg['locale'].split('-')[0]}"]
    if cfg["block_webrtc"]:
        args += ["--disable-non-proxied-udp", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    if paths:
        joined = ",".join(paths)
        args += [f"--load-extension={joined}", f"--disable-extensions-except={joined}"]
    return args + os.environ.get("PM_CHROMIUM_EXTRA_ARGS", "").split()


def open_start_page(page, url: str):
    """Strona startowa nie może zabić przeglądarki: błąd nawigacji (brak sieci, zły proxy, timeout, literówka w adresie)
    kończy się stroną błędu w Chrome, a nie wyjątkiem, który zamykałby cały proces (dawniej goto() rzucał i kończył workera)."""
    url = core.normalize_url(url)
    if not url or url == "about:blank":
        return
    try:
        page.goto(url, wait_until="commit", timeout=60000)
    except Exception as e:
        print("Strona startowa nie otworzyła się (przeglądarka działa dalej):", type(e).__name__, str(e)[:200], flush=True)


def run_chrome(pid: str, cfg: dict):
    """fingerprint-chromium (maskowanie w binarce) sterowany przez Playwright. Seed fingerprintu jest w configu
    profilu, więc na obu komputerach profil wygląda tak samo."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    from . import chromium

    exe = chromium.exe_path()
    if not exe:
        raise RuntimeError("Brak silnika Chrome. Pobierz go w Ustawieniach.")
    data = core.data_dir(pid)
    try:   # zakładki trzeba wpisać do profilu, zanim Chrome go otworzy (potem nadpisałby plik przy zamknięciu)
        bookmarks.seed(data, cfg.get("bookmarks") or [])
    except Exception as e:
        print("Nie dodano zakładek:", type(e).__name__, e, flush=True)
    paths = [str(addons.path(s).resolve()) for s in cfg["addons"]
             if (addons.path(s) / "manifest.json").exists()]
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(data), executable_path=str(exe), headless=False, viewport=None,
            chromium_sandbox=True,    # inaczej Playwright dokleja --no-sandbox i Chrome pokazuje ostrzeżenie o bezpieczeństwie
            ignore_default_args=["--enable-automation", "--disable-extensions"],
            args=chrome_args(pid, cfg, paths))

        # Chrome na Windows szyfruje bazę cookies kluczem powiązanym z użytkownikiem/komputerem, więc po
        # przeniesieniu profilu na drugi komputer by się nie odszyfrowała. Dlatego trzymamy kopię w JSON-ie.
        cf = data / COOKIES_FILE
        if cf.exists():
            try:
                cookies = json.loads(cf.read_text("utf-8"))
                try:
                    ctx.add_cookies(cookies)
                except Exception:
                    for c in cookies:
                        try:
                            ctx.add_cookies([c])
                        except Exception:
                            pass
            except Exception as e:
                print("Nie wczytano cookies:", e)

        def save():
            try:
                tmp = cf.with_suffix(".tmp")
                tmp.write_text(json.dumps(ctx.cookies()), "utf-8")
                tmp.replace(cf)
            except Exception as e:
                print("save cookies:", type(e).__name__, str(e)[:200], flush=True)   # np. przeglądarka już zamknięta

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        open_start_page(page, cfg["start_url"])
        while True:
            try:
                ctx.wait_for_event("close", timeout=8000)
                break
            except PWTimeout:
                save()
            except Exception as e:
                print("koniec petli:", type(e).__name__, str(e)[:200], flush=True)
                break
