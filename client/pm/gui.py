import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressDialog, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)

from . import addons, core, sync
from .api import Api, ApiError

ERRORS = {
    "invalid_credentials": "Nieprawidłowy login lub hasło.", "totp_required": "Podaj kod 2FA.",
    "invalid_totp": "Nieprawidłowy kod 2FA.", "too_many_attempts": "Za dużo prób, spróbuj za kilka minut.",
    "registration_closed": "Rejestracja jest na tym serwerze zamknięta.", "invalid_invite": "Nieprawidłowy kod zaproszenia.",
    "username_taken": "Ta nazwa użytkownika jest zajęta.", "user_limit_reached": "Serwer osiągnął limit użytkowników.",
    "profile_too_large": "Profil przekracza limit rozmiaru na serwerze.", "quota_exceeded": "Przekroczono limit miejsca na serwerze.",
    "profile_limit_reached": "Osiągnięto limit liczby profili.", "no_server": "Nie podano adresu serwera.",
    "revision_conflict": "Konflikt wersji: na serwerze jest nowsza wersja profilu.",
    "lock_lost": "Utracono blokadę profilu (serwer przejął ją po przerwie w łączności).",
    "checksum_mismatch": "Pobrany profil jest uszkodzony (zła suma kontrolna).",
}


def err_text(e: Exception) -> str:
    if isinstance(e, ApiError):
        if e.code == "locked":
            return f"Profil jest teraz używany na urządzeniu: {e.data.get('device_name') or '?'}."
        if e.code == "network":
            return f"Brak połączenia z serwerem. {e.data.get('msg', '')}"
        if e.status == 401 and e.code in ("invalid_token", "not_authenticated"):
            return "Sesja wygasła, zaloguj się ponownie."
        return ERRORS.get(e.code, f"Błąd serwera: {e.code}")
    return str(e)


class Task(QThread):
    done, failed, progress = Signal(object), Signal(object), Signal(str, int)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(lambda t, p=-1: self.progress.emit(t, p)))
        except Exception as e:  # noqa
            self.failed.emit(e)


def busy_dialog(parent, text="Pracuję…") -> QProgressDialog:
    d = QProgressDialog(text, None, 0, 0, parent)
    d.setWindowModality(Qt.WindowModal)
    d.setMinimumDuration(0)
    d.setWindowTitle("Profile Manager")
    d.setCancelButton(None)
    return d


# ----------------------------------------------------------------- logowanie
class LoginDialog(QDialog):
    def __init__(self, parent, api: Api):
        super().__init__(parent)
        self.api, self.s = api, api.s
        self.setWindowTitle("Logowanie")
        self.setMinimumWidth(380)
        self.url = QLineEdit(self.s.get("server_url", ""))
        self.url.setPlaceholderText("https://moj-serwer.example.com")
        self.user = QLineEdit(self.s.get("username", ""))
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.Password)
        self.totp = QLineEdit(); self.totp.setPlaceholderText("kod 2FA (jeśli włączone)")
        self.invite = QLineEdit(); self.invite.setPlaceholderText("kod zaproszenia (tylko rejestracja)")
        form = QFormLayout(self)
        for label, w in (("Serwer", self.url), ("Użytkownik", self.user), ("Hasło", self.pw),
                         ("2FA", self.totp), ("Zaproszenie", self.invite)):
            form.addRow(label, w)
        row = QHBoxLayout()
        b_login, b_reg, b_local = QPushButton("Zaloguj"), QPushButton("Załóż konto"), QPushButton("Tylko lokalnie")
        b_login.setDefault(True)
        for b in (b_login, b_reg, b_local):
            row.addWidget(b)
        form.addRow(row)
        b_login.clicked.connect(lambda: self.go(False))
        b_reg.clicked.connect(lambda: self.go(True))
        b_local.clicked.connect(self.reject)

    def go(self, register: bool):
        self.s["server_url"] = self.url.text().strip()
        try:
            if register:
                res = self.api.register(self.user.text(), self.pw.text(), self.invite.text() or None)
            else:
                res = self.api.login(self.user.text(), self.pw.text(), self.totp.text() or None)
        except ApiError as e:
            QMessageBox.warning(self, "Logowanie", err_text(e))
            return
        self.s.update(token=res["token"], username=res["username"])
        core.save_settings(self.s)
        self.accept()


# ----------------------------------------------------------------- 2FA
class SecurityDialog(QDialog):
    def __init__(self, parent, api: Api):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Zabezpieczenia konta (2FA)")
        self.setMinimumWidth(420)
        self.lay = QVBoxLayout(self)
        self.render()

    def clear(self):
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def render(self):
        self.clear()
        try:
            me = self.api.me()
        except ApiError as e:
            self.lay.addWidget(QLabel(err_text(e)))
            return
        self.lay.addWidget(QLabel(f"Zalogowano jako <b>{me['username']}</b>"))
        if me["totp_enabled"]:
            self.lay.addWidget(QLabel(f"2FA jest włączone. Pozostałe kody zapasowe: {me['backup_codes_left']}"))
            b = QPushButton("Wyłącz 2FA"); b.clicked.connect(self.disable); self.lay.addWidget(b)
        else:
            self.lay.addWidget(QLabel("2FA jest wyłączone."))
            b = QPushButton("Włącz 2FA"); b.clicked.connect(self.setup); self.lay.addWidget(b)

    def setup(self):
        try:
            data = self.api.totp_setup()
        except ApiError as e:
            return QMessageBox.warning(self, "2FA", err_text(e))
        import qrcode
        img = qrcode.make(data["uri"]).convert("RGBA").resize((220, 220))
        qimg = QImage(img.tobytes("raw", "RGBA"), img.width, img.height, QImage.Format_RGBA8888).copy()
        self.clear()
        self.lay.addWidget(QLabel("Zeskanuj kod w aplikacji uwierzytelniającej\n(Google Authenticator, Aegis, 2FAS…)"))
        qr = QLabel(); qr.setPixmap(QPixmap.fromImage(qimg)); qr.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(qr)
        sec = QLineEdit(data["secret"]); sec.setReadOnly(True); self.lay.addWidget(sec)
        self.code = QLineEdit(); self.code.setPlaceholderText("6-cyfrowy kod z aplikacji")
        self.lay.addWidget(self.code)
        b = QPushButton("Potwierdź i włącz"); b.clicked.connect(self.enable); self.lay.addWidget(b)

    def enable(self):
        try:
            codes = self.api.totp_enable(self.code.text())["backup_codes"]
        except ApiError as e:
            return QMessageBox.warning(self, "2FA", err_text(e))
        QMessageBox.information(self, "Kody zapasowe",
                                "Zapisz te kody w bezpiecznym miejscu (każdy działa raz):\n\n" + "\n".join(codes))
        self.render()

    def disable(self):
        pw, ok = QInputDialog.getText(self, "Wyłącz 2FA", "Hasło:", QLineEdit.Password)
        if not ok:
            return
        code, ok = QInputDialog.getText(self, "Wyłącz 2FA", "Kod 2FA lub kod zapasowy:")
        if not ok:
            return
        try:
            self.api.totp_disable(pw, code)
        except ApiError as e:
            return QMessageBox.warning(self, "2FA", err_text(e))
        self.render()


# ----------------------------------------------------------------- rozszerzenia
class AddonsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Biblioteka rozszerzeń")
        self.setMinimumSize(460, 380)
        lay = QVBoxLayout(self)
        self.list = QListWidget(); lay.addWidget(self.list)
        row = QHBoxLayout()
        for name, slug in addons.PRESETS.items():
            b = QPushButton(f"+ {name}"); b.clicked.connect(lambda _=False, s=slug: self.install(s)); row.addWidget(b)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        for text, fn in (("Zainstaluj ze sluga / linku AMO…", self.install_custom),
                         ("Aktualizuj wszystkie", self.update_all), ("Usuń zaznaczone", self.remove)):
            b = QPushButton(text); b.clicked.connect(fn); row2.addWidget(b)
        lay.addLayout(row2)
        self.refresh()
        self._tasks = []

    def refresh(self):
        self.list.clear()
        for a in addons.installed():
            it = QListWidgetItem(f"{a['name']}  v{a['version']}  [{a['slug']}]")
            it.setData(Qt.UserRole, a["slug"])
            self.list.addItem(it)

    def run(self, fn):
        dlg = busy_dialog(self)
        t = Task(fn)
        t.progress.connect(lambda txt, p: (dlg.setLabelText(txt), dlg.setRange(0, 100 if p >= 0 else 0), dlg.setValue(max(p, 0))))
        t.done.connect(lambda _: (dlg.close(), self.refresh()))
        t.failed.connect(lambda e: (dlg.close(), QMessageBox.warning(self, "Rozszerzenia", str(e))))
        self._tasks.append(t); t.start(); dlg.show()

    def install(self, slug): self.run(lambda pr: addons.install(slug, pr))

    def install_custom(self):
        text, ok = QInputDialog.getText(self, "Instalacja", "Slug lub link (np. https://addons.mozilla.org/firefox/addon/ublock-origin/):")
        if ok and text.strip():
            self.install(text)

    def update_all(self):
        slugs = [a["slug"] for a in addons.installed()]
        self.run(lambda pr: [addons.install(s, pr) for s in slugs])

    def remove(self):
        for it in self.list.selectedItems():
            addons.remove(it.data(Qt.UserRole))
        self.refresh()


# ----------------------------------------------------------------- edycja profilu
class ProfileDialog(QDialog):
    def __init__(self, parent, meta: dict | None):
        super().__init__(parent)
        self.setWindowTitle("Edycja profilu" if meta else "Nowy profil")
        self.setMinimumWidth(460)
        cfg = core.merged_config(meta) if meta else dict(core.DEFAULT_CONFIG)
        self.orig_os = cfg["os"] if meta else None
        self.name = QLineEdit(meta["name"] if meta else "")
        self.os = QComboBox(); self.os.addItems(["windows", "macos", "linux"]); self.os.setCurrentText(cfg["os"])
        self.locale = QLineEdit(cfg["locale"]); self.locale.setPlaceholderText("np. pl-PL, en-US (puste = domyślny)")
        self.tz = QLineEdit(cfg["timezone"]); self.tz.setPlaceholderText("np. Europe/Warsaw (puste = z systemu)")
        self.url = QLineEdit(cfg["start_url"])
        self.webrtc = QCheckBox("Zablokuj WebRTC (wyciek IP)"); self.webrtc.setChecked(cfg["block_webrtc"])
        self.human = QCheckBox("Humanizuj ruchy myszy"); self.human.setChecked(cfg["humanize"])
        self.ubo = QCheckBox("uBlock Origin (wbudowany)"); self.ubo.setChecked(cfg["ublock"])
        self.sync = QCheckBox("Synchronizuj z serwerem"); self.sync.setChecked(cfg["sync"])
        self.regen = QCheckBox("Wygeneruj nowy fingerprint przy zapisie")
        self.notes = QPlainTextEdit(cfg["notes"]); self.notes.setFixedHeight(60)
        self.addons = QListWidget(); self.addons.setFixedHeight(90)
        for a in addons.installed():
            it = QListWidgetItem(f"{a['name']} [{a['slug']}]")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if a["slug"] in cfg["addons"] else Qt.Unchecked)
            it.setData(Qt.UserRole, a["slug"]); self.addons.addItem(it)
        # slugi spoza lokalnej biblioteki (np. profil z drugiego komputera) zachowujemy
        self.extra_addons = [s for s in cfg["addons"] if s not in {a["slug"] for a in addons.installed()}]
        f = QFormLayout(self)
        for label, w in (("Nazwa", self.name), ("System w fingerprincie", self.os), ("Język", self.locale),
                         ("Strefa czasowa", self.tz), ("Strona startowa", self.url), ("Rozszerzenia", self.addons),
                         ("", self.webrtc), ("", self.human), ("", self.ubo), ("", self.sync),
                         ("Notatki", self.notes)):
            f.addRow(label, w)
        if meta:
            f.addRow("", self.regen)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

    def result_config(self) -> tuple[str, dict, bool]:
        slugs = [self.addons.item(i).data(Qt.UserRole) for i in range(self.addons.count())
                 if self.addons.item(i).checkState() == Qt.Checked] + self.extra_addons
        cfg = {"os": self.os.currentText(), "locale": self.locale.text().strip(), "timezone": self.tz.text().strip(),
               "start_url": self.url.text().strip() or "about:blank", "block_webrtc": self.webrtc.isChecked(),
               "humanize": self.human.isChecked(), "ublock": self.ubo.isChecked(), "addons": slugs,
               "sync": self.sync.isChecked(), "notes": self.notes.toPlainText()}
        regen = self.regen.isChecked() or (self.orig_os is not None and cfg["os"] != self.orig_os)
        return self.name.text().strip() or "Profil", cfg, regen


# ----------------------------------------------------------------- okno główne
class MainWindow(QMainWindow):
    COLS = ["Nazwa", "System", "Rozszerzenia", "Sync", "Status"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Profile Manager")
        self.resize(900, 460)
        self.s = core.load_settings()
        self.api = Api(self.s)
        self.server: dict[str, dict] = {}       # profile z serwera (id -> dane)
        self.online = False
        self.running: dict[str, dict] = {}      # id -> {proc, last_hb, hb_ok}
        self.pending: dict[str, str] = {}       # id -> komunikat (czeka na wysyłkę)
        self.busy: set[str] = set()
        self.pending_hb: dict[str, float] = {}
        self._tasks: list[Task] = []
        self.browser_ready = False

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.doubleClicked.connect(self.launch)
        box = QWidget(); lay = QVBoxLayout(box); lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Nowy", self.new), ("Uruchom", self.launch), ("Edytuj", self.edit), ("Usuń", self.delete),
                         ("Rozszerzenia", self.open_addons), ("2FA / konto", self.open_security),
                         ("Odśwież", self.refresh), ("Zaloguj / zmień serwer", self.login)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        lay.addLayout(row)
        self.setCentralWidget(box)
        self.statusBar().showMessage("Start…")

        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(1000)
        QTimer.singleShot(0, self.startup)

    # ---- pomocnicze
    def run(self, fn, done=None, failed=None, title="Pracuję…", modal=True):
        dlg = busy_dialog(self, title) if modal else None
        t = Task(fn)
        def p(txt, pct):
            self.statusBar().showMessage(txt)
            if dlg:
                dlg.setLabelText(txt); dlg.setRange(0, 100 if pct >= 0 else 0); dlg.setValue(max(pct, 0))
        def ok(r):
            if dlg: dlg.close()
            self._tasks.remove(t)
            if done: done(r)
        def fail(e):
            if dlg: dlg.close()
            self._tasks.remove(t)
            if failed: failed(e)
            else: self.fail(e)
        t.progress.connect(p); t.done.connect(ok); t.failed.connect(fail)
        self._tasks.append(t); t.start()
        if dlg: dlg.show()

    def fail(self, e: Exception):
        QMessageBox.warning(self, "Profile Manager", err_text(e))
        if isinstance(e, ApiError) and e.status == 401:
            self.login()

    def selected(self) -> dict | None:
        r = self.table.currentRow()
        if r < 0:
            return None
        return core.load_meta(self.table.item(r, 0).data(Qt.UserRole))

    def has_token(self) -> bool:
        return bool(self.s.get("token") and self.s.get("server_url"))

    # ---- start
    def startup(self):
        def ensure_browser(pr):
            from camoufox.pkgman import CamoufoxFetcher, installed_verstr
            try:
                installed_verstr()
            except Exception:
                pr("Pobieram przeglądarkę Camoufox (jednorazowo, ok. 100–200 MB)…", -1)
                CamoufoxFetcher().install()
        self.run(ensure_browser, done=lambda _: self.set_ready(), title="Przygotowuję przeglądarkę…",
                 failed=lambda e: (self.set_ready(), QMessageBox.warning(self, "Camoufox", f"Nie udało się pobrać przeglądarki:\n{e}")))
        if not self.has_token():
            self.login()
        self.refresh()

    def set_ready(self):
        self.browser_ready = True

    def login(self):
        if LoginDialog(self, self.api).exec():
            self.refresh()

    # ---- lista
    def refresh(self):
        if not self.has_token():
            self.online = False
            self.fill()
            return
        def fetch(_): return self.api.list_profiles()
        def ok(rows):
            self.online = True
            self.server = {r["id"]: r for r in rows}
            for r in rows:            # profile z innych komputerów pojawiają się lokalnie (bez danych, dane pobierze Uruchom)
                m = core.load_meta(r["id"])
                if m is None:
                    core.save_meta({"id": r["id"], "name": r["name"], "config": r["config"], "revision": 0, "dirty": False})
                elif r["id"] not in self.running and r["id"] not in self.pending:
                    m["name"], m["config"] = r["name"], r["config"]
                    core.save_meta(m)
            self.fill()
        def bad(e):
            self.online = False
            self.statusBar().showMessage("Tryb offline: " + err_text(e))
            if isinstance(e, ApiError) and e.status == 401:
                self.s["token"] = ""; core.save_settings(self.s)
            self.fill()
        self.run(fetch, ok, bad, modal=False)

    def fill(self):
        keep = self.table.currentRow()
        metas = core.list_local()
        self.table.setRowCount(len(metas))
        for i, m in enumerate(metas):
            cfg = core.merged_config(m)
            srv = self.server.get(m["id"])
            if m["id"] in self.running:
                st = "▶ uruchomiony"
            elif m["id"] in self.pending:
                st = "⏳ czeka na wysyłkę: " + self.pending[m["id"]]
            elif m["id"] in self.busy:
                st = "… trwa operacja"
            elif cfg["sync"] and self.online and srv is None:
                st = "brak na serwerze (zapisz profil, by wysłać)"
            elif srv and srv["lock"]["active"] and srv["lock"]["device_id"] != self.s["device_id"]:
                st = f"🔒 używany na: {srv['lock']['device_name']}"
            elif cfg["sync"] and srv and srv["revision"] != m["revision"]:
                st = "⬇ nowsza wersja na serwerze"
            else:
                st = "✓ gotowy"
            sync_txt = ("rew. %d" % m["revision"]) if cfg["sync"] else "tylko lokalnie"
            for j, v in enumerate([m["name"], cfg["os"], ", ".join(cfg["addons"]) or "—", sync_txt, st]):
                it = QTableWidgetItem(v); it.setData(Qt.UserRole, m["id"]); self.table.setItem(i, j, it)
        if 0 <= keep < len(metas):
            self.table.selectRow(keep)

    # ---- CRUD
    def new(self):
        d = ProfileDialog(self, None)
        if not d.exec():
            return
        name, cfg, _ = d.result_config()
        if cfg["sync"] and not self.has_token():
            return QMessageBox.information(self, "Sync", "Zaloguj się, aby synchronizować profil, albo odznacz „Synchronizuj”.")
        def job(pr):
            pr("Generuję fingerprint (przy pierwszym razie pobiera model, chwilę trwa)…", -1)
            meta = core.create_local(name, cfg)
            if cfg["sync"]:
                try:
                    self.api.create_profile(meta["id"], name, meta["config"], core.read_fp_encoded(meta["id"]))
                except Exception:
                    core.delete_local(meta["id"])
                    raise
            return meta
        self.run(job, lambda _: self.refresh(), title="Tworzę profil…")

    def edit(self):
        meta = self.selected()
        if not meta or meta["id"] in self.running or meta["id"] in self.pending:
            return
        d = ProfileDialog(self, meta)
        if not d.exec():
            return
        name, cfg, regen = d.result_config()
        pid = meta["id"]
        def job(pr):
            fp = None
            if regen:
                pr("Generuję nowy fingerprint…", -1)
                fp = core.encode_fp(core.generate_fp(cfg["os"]))
            if cfg["sync"] and self.has_token():
                if pid in self.server:
                    kw = {"name": name, "config": cfg}
                    if fp: kw["fingerprint"] = fp
                    self.api.update_profile(pid, **kw)
                else:
                    self.api.create_profile(pid, name, cfg, fp or core.read_fp_encoded(pid))
            if fp:
                core.write_fp_encoded(pid, fp)
            meta.update(name=name, config=cfg)
            core.save_meta(meta)
        self.run(job, lambda _: self.refresh(), title="Zapisuję…")

    def delete(self):
        meta = self.selected()
        if not meta or meta["id"] in self.running or meta["id"] in self.pending:
            return
        if QMessageBox.question(self, "Usuń", f"Usunąć profil „{meta['name']}” lokalnie i na serwerze?\nTego nie da się cofnąć.") != QMessageBox.Yes:
            return
        def job(_):
            if meta["id"] in self.server and self.has_token():
                self.api.delete_profile(meta["id"])
            core.delete_local(meta["id"])
        self.run(job, lambda _: self.refresh())

    def open_addons(self): AddonsDialog(self).exec()

    def open_security(self):
        if self.has_token():
            SecurityDialog(self, self.api).exec()
        else:
            self.login()

    # ---- uruchamianie
    def launch(self):
        meta = self.selected()
        if not meta:
            return
        pid = meta["id"]
        if pid in self.running or pid in self.busy:
            return
        if pid in self.pending:
            return self.retry_finish(pid)
        if not self.browser_ready:
            return QMessageBox.information(self, "Profile Manager", "Przeglądarka jeszcze się pobiera, poczekaj chwilę.")
        cfg = core.merged_config(meta)
        if cfg["sync"] and not self.has_token():
            return QMessageBox.information(self, "Sync", "Ten profil jest synchronizowany. Zaloguj się, żeby go uruchomić.")
        self.busy.add(pid); self.fill()
        def job(pr):
            addons.ensure(cfg["addons"], pr)
            warn = sync.prepare(self.api, pid, pr)
            return warn
        def ok(warn):
            self.busy.discard(pid)
            if warn:
                QMessageBox.information(self, "Uwaga", warn)
            cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")]
            log = open(core.LOGS / f"{pid}.log", "ab")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(cmd + ["--worker", pid], stdout=log, stderr=log, creationflags=flags)
            self.running[pid] = {"proc": proc, "last_hb": time.time()}
            self.fill()
        def bad(e):
            self.busy.discard(pid); self.fill(); self.fail(e)
            self.refresh()
        self.run(job, ok, bad, title="Przygotowuję profil…")

    def tick(self):
        now = time.time()
        for pid, r in list(self.running.items()):
            if r["proc"].poll() is not None:
                del self.running[pid]
                self.retry_finish(pid)
            elif now - r["last_hb"] > 30:
                r["last_hb"] = now
                self.heartbeat(pid)
        for pid in list(self.pending):
            if now - self.pending_hb.get(pid, 0) > 30:
                self.pending_hb[pid] = now
                self.heartbeat(pid)

    def heartbeat(self, pid):
        if not core.merged_config(core.load_meta(pid) or {})["sync"] or not self.has_token():
            return
        t = Task(lambda _: self.api.heartbeat(pid))
        t.failed.connect(lambda e, t=t: (self._tasks.remove(t), self.statusBar().showMessage("Heartbeat: " + err_text(e))))
        t.done.connect(lambda _, t=t: self._tasks.remove(t))
        self._tasks.append(t); t.start()

    def retry_finish(self, pid):
        self.busy.add(pid); self.pending.pop(pid, None); self.fill()
        def ok(_):
            self.busy.discard(pid); self.pending.pop(pid, None); self.refresh()
            self.statusBar().showMessage("Profil zsynchronizowany.")
        def bad(e):
            self.busy.discard(pid)
            self.pending[pid] = err_text(e)
            self.fill()
            self.statusBar().showMessage("Nie udało się wysłać profilu. Kliknij Uruchom na tym profilu, aby ponowić.")
        self.run(lambda pr: sync.finish(self.api, pid, pr), ok, bad, modal=False)

    def closeEvent(self, ev):
        if self.running or self.pending or self.busy:
            QMessageBox.warning(self, "Profile Manager", "Zamknij najpierw przeglądarki i poczekaj na zakończenie synchronizacji.")
            ev.ignore()
        else:
            ev.accept()


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
