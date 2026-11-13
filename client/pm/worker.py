"""Proces przeglądarki: uruchamiany jako osobny proces (exe --worker <id>), żeby GUI się nie blokowało."""
from . import addons, core


def run(pid: str):
    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox

    meta = core.load_meta(pid)
    cfg = core.merged_config(meta)
    fp = core.decode_fp(core.read_fp_encoded(pid))
    paths = [str(addons.path(s)) for s in cfg["addons"] if (addons.path(s) / "manifest.json").exists()]

    opts = dict(
        persistent_context=True,
        user_data_dir=str(core.data_dir(pid)),
        headless=False,
        fingerprint=fp,                 # ten sam fingerprint na każdym komputerze
        i_know_what_im_doing=True,
        addons=paths or None,
        exclude_addons=[] if cfg["ublock"] else [DefaultAddons.UBO],
        block_webrtc=bool(cfg["block_webrtc"]),
        humanize=True if cfg["humanize"] else None,
        locale=cfg["locale"] or None,
        config={"timezone": cfg["timezone"]} if cfg["timezone"] else None,
    )
    opts = {k: v for k, v in opts.items() if v is not None}
    with Camoufox(**opts) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if cfg["start_url"] and cfg["start_url"] != "about:blank":
            page.goto(cfg["start_url"])
        ctx.wait_for_event("close", timeout=0)   # czekamy, aż użytkownik zamknie przeglądarkę
