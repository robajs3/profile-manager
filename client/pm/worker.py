"""Proces przeglądarki: uruchamiany jako osobny proces (exe --worker <id>), żeby GUI się nie blokowało."""
import json
import os
import time

from . import addons, core


def _cfg(pid):
    meta = core.load_meta(pid)
    return core.merged_config(meta)


def run(pid: str):
    cfg = _cfg(pid)
    (run_chrome if cfg["engine"] == "chrome" else run_firefox)(pid, cfg)


def run_firefox(pid: str, cfg: dict):
    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox

    fp = core.decode_fp(core.read_fp_encoded(pid))
    paths = [str(addons.path(s, "firefox")) for s in cfg["addons"]
             if (addons.path(s, "firefox") / "manifest.json").exists()]
    opts = dict(
        persistent_context=True, user_data_dir=str(core.data_dir(pid)), headless=False,
        fingerprint=fp,                 # ten sam fingerprint na każdym komputerze
        i_know_what_im_doing=True, addons=paths or None,
        exclude_addons=[] if cfg["ublock"] else [DefaultAddons.UBO],
        block_webrtc=bool(cfg["block_webrtc"]), humanize=True if cfg["humanize"] else None,
        locale=cfg["locale"] or None,
        config={"timezone": cfg["timezone"]} if cfg["timezone"] else None,
    )
    with Camoufox(**{k: v for k, v in opts.items() if v is not None}) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if cfg["start_url"] and cfg["start_url"] != "about:blank":
            page.goto(cfg["start_url"])
        ctx.wait_for_event("close", timeout=0)


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
    paths = [str(addons.path(s, "chrome").resolve()) for s in cfg["addons"]
             if (addons.path(s, "chrome") / "manifest.json").exists()]
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
        if cfg["start_url"] and cfg["start_url"] != "about:blank":
            page.goto(cfg["start_url"])
        while True:
            try:
                ctx.wait_for_event("close", timeout=8000)
                break
            except PWTimeout:
                save()
            except Exception as e:
                print("koniec petli:", type(e).__name__, str(e)[:200], flush=True)
                break
