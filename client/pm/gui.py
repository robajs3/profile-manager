import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressDialog, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)

import random

from . import addons, chromium, core, sync
from .api import Api, ApiError

ERRORS = {
    "invalid_credentials": "Nieprawidłowy login lub hasło.", "totp_required": "Podaj kod 2FA.",
    "invalid_totp": "Nieprawidłowy kod 2FA.", "too_many_attempts": "Za dużo prób, spróbuj za kilka minut.",
    "registration_closed": "Rejestracja jest na tym serwerze zamknięta.", "invalid_invite": "Nieprawidłowy kod zaproszenia.",
    "username_taken": "Ta nazwa użytkownika jest zajęta.", "user_limit_reached": "Serwer osiągnął limit użytkowników.",
    "profile_too_large": "Profil przekracza limit rozmiaru na serwerze.", "quota_exceeded": "Przekroczono limit miejsca na serwerze.",
    "profile_limit_reached": "Osiągnięto limit liczby profili.", "no_server": "Nie podano adresu serwera.",
    "revision_conflict": "Konflikt wersji: na serwerze jest nowsza wersja profilu.",
    "lock_required": "Serwer nie widzi Twojej blokady na tym profilu (wygasła podczas wysyłki). Dane lokalne są bezpieczne, spróbuj ponownie.",
    "lock_lost": "Utracono blokadę profilu (serwer przejął ją po przerwie w łączności).",
    "checksum_mismatch": "Pobrany profil jest uszkodzony (zła suma kontrolna).",
}


def err_text(e: Exception) -> str:
    if isinstance(e, ApiError):
        if e.code == "locked":
            return f"Profil jest teraz używany na urządzeniu: {e.data.get('device_name') or '?'}."
        if e.code == "network":
            m = e.data.get("msg", "")
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in m or "certificate" in m.lower():
                hint = "\n\nProblem z certyfikatem: sprawdź datę/godzinę komputera i czy antywirus nie skanuje HTTPS."
            return f"Brak połączenia z serwerem. {m}{hint}"
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


# ----------------------------------------------------------------- pomocnicze
def ensure_camoufox(progress):
    from camoufox.pkgman import CamoufoxFetcher, installed_verstr
    try:
        installed_verstr()
    except Exception:
        progress("Pobieram przeglądarkę Camoufox (jednorazowo, ok. 100–200 MB)…", -1)
        CamoufoxFetcher().install()


def ensure_chrome_engine(progress):
    chromium.ensure(progress)


# ----------------------------------------------------------------- rozszerzenia
class AddonsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Biblioteka rozszerzeń")
        self.setMinimumSize(500, 420)
        self._tasks = []
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Silnik:"))
        self.engine = QComboBox()
        for k, v in core.ENGINES.items():
            self.engine.addItem(v, k)
        self.engine.currentIndexChanged.connect(self.rebuild)
        top.addWidget(self.engine, 1)
        lay.addLayout(top)
        self.list = QListWidget(); lay.addWidget(self.list)
        self.presets = QHBoxLayout(); lay.addLayout(self.presets)
        row2 = QHBoxLayout()
        for text, fn in (("Zainstaluj ze sluga / ID / linku…", self.install_custom),
                         ("Aktualizuj wszystkie", self.update_all), ("Usuń zaznaczone", self.remove)):
            b = QPushButton(text); b.clicked.connect(fn); row2.addWidget(b)
        lay.addLayout(row2)
        row3 = QHBoxLayout()
        for text, fn in (("Dodaj z pliku (.crx / .zip / .xpi)…", self.add_file), ("Dodaj z folderu…", self.add_dir)):
            b = QPushButton(text); b.clicked.connect(fn); row3.addWidget(b)
        lay.addLayout(row3)
        hint = QLabel("Gdy pobieranie ze sklepu nie działa: zainstaluj rozszerzenie w zwykłym Chrome i wskaż jego folder "
                      "(…\\Chrome\\User Data\\Default\\Extensions\\<id>\\<wersja>).")
        hint.setWordWrap(True); lay.addWidget(hint)
        self.rebuild()

    def eng(self) -> str:
        return self.engine.currentData()

    def rebuild(self):
        while self.presets.count():
            w = self.presets.takeAt(0).widget()
            if w:
                w.deleteLater()
        for name, ref in addons.PRESETS[self.eng()].items():
            b = QPushButton(f"+ {name}"); b.clicked.connect(lambda _=False, r=ref: self.install(r)); self.presets.addWidget(b)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for a in addons.installed(self.eng()):
            it = QListWidgetItem(f"{a['name']}  v{a['version']}  [{a['slug']}]")
            it.setData(Qt.UserRole, a["slug"]); self.list.addItem(it)

    def run(self, fn):
        dlg = busy_dialog(self)
        t = Task(fn)
        t.progress.connect(lambda txt, p: (dlg.setLabelText(txt), dlg.setRange(0, 100 if p >= 0 else 0), dlg.setValue(max(p, 0))))
        t.done.connect(lambda _: (dlg.close(), self.refresh()))
        t.failed.connect(lambda e: (dlg.close(), self.refresh(), QMessageBox.warning(self, "Rozszerzenia", str(e))))
        self._tasks.append(t); t.start(); dlg.show()

    def install(self, ref):
        eng = self.eng(); self.run(lambda pr: addons.install(ref, eng, pr))

    def install_custom(self):
        hint = "ID lub link z Chrome Web Store:" if self.eng() == "chrome" else "Slug lub link z addons.mozilla.org:"
        text, ok = QInputDialog.getText(self, "Instalacja", hint)
        if ok and text.strip():
            self.install(text)

    def add_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "Wybierz rozszerzenie", "", "Rozszerzenia (*.crx *.zip *.xpi);;Wszystkie pliki (*)")
        if f:
            eng = self.eng(); self.run(lambda pr: addons.install_from_file(f, eng, pr))

    def add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Wybierz folder z rozpakowanym rozszerzeniem (z manifest.json)")
        if d:
            eng = self.eng(); self.run(lambda pr: addons.install_from_file(d, eng, pr))

    def update_all(self):
        eng = self.eng(); slugs = [a["slug"] for a in addons.installed(eng)]
        self.run(lambda pr: [addons.install(s, eng, pr) for s in slugs])

    def remove(self):
        for it in self.list.selectedItems():
            addons.remove(it.data(Qt.UserRole), self.eng())
        self.refresh()


# ----------------------------------------------------------------- ustawienia silnika Chrome
class SettingsDialog(QDialog):
    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.s, self._tasks = settings, []
        self.setWindowTitle("Ustawienia")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Silnik Chrome (fingerprint-chromium)</b><br>Otwartoźródłowy Chromium z maskowaniem fingerprintu "
                             "w binarce. Bez kluczy i bez limitu jednoczesnych sesji."))
        self.status = QLabel(); lay.addWidget(self.status)
        b = QPushButton("Pobierz / zaktualizuj silnik Chrome"); b.clicked.connect(self.download); lay.addWidget(b)
        self.refresh()

    def refresh(self):
        v = chromium.version()
        self.status.setText(f"Zainstalowano: {v}" if v else ("Własna binarka (PM_CHROMIUM_EXE)" if chromium.exe_path() else "Nie zainstalowano (pobierze się też automatycznie przy pierwszym uruchomieniu profilu Chrome)."))

    def download(self):
        dlg = busy_dialog(self, "Pobieram silnik Chrome…")
        t = Task(lambda pr: chromium.download_latest(pr))
        t.progress.connect(lambda txt, p: (dlg.setLabelText(txt), dlg.setRange(0, 100 if p >= 0 else 0), dlg.setValue(max(p, 0))))
        t.done.connect(lambda _: (dlg.close(), self.refresh()))
        t.failed.connect(lambda e: (dlg.close(), QMessageBox.warning(self, "Silnik Chrome", str(e))))
        self._tasks.append(t); t.start(); dlg.show()


# ----------------------------------------------------------------- edycja profilu
class ProfileDialog(QDialog):
    def __init__(self, parent, meta: dict | None):
        super().__init__(parent)
        self.setWindowTitle("Edycja profilu" if meta else "Nowy profil")
        self.setMinimumWidth(480)
        cfg = core.merged_config(meta) if meta else dict(core.DEFAULT_CONFIG)
        self.existing, self.cfg0 = meta is not None, cfg
        self.orig_os = cfg["os"] if meta else None
        self.name = QLineEdit(meta["name"] if meta else "")
        self.engine = QComboBox()
        for k, v in core.ENGINES.items():
            self.engine.addItem(v, k)
        self.engine.setCurrentIndex(self.engine.findData(cfg["engine"]))
        self.engine.setEnabled(not self.existing)     # silnika nie da się zmienić (formaty profili są niekompatybilne)
        self.os = QComboBox(); self.os.addItems(["windows", "macos", "linux"]); self.os.setCurrentText(cfg["os"])
        self.locale = QLineEdit(cfg["locale"]); self.locale.setPlaceholderText("np. pl-PL, en-US (puste = domyślny)")
        self.tz = QLineEdit(cfg["timezone"]); self.tz.setPlaceholderText("np. Europe/Warsaw (puste = z systemu)")
        self.url = QLineEdit(cfg["start_url"])
        self.webrtc = QCheckBox("Zablokuj WebRTC (wyciek IP)"); self.webrtc.setChecked(cfg["block_webrtc"])
        self.human = QCheckBox("Humanizuj ruchy myszy (tylko Firefox)"); self.human.setChecked(cfg["humanize"])
        self.ubo = QCheckBox("uBlock Origin (wbudowany, tylko Firefox)"); self.ubo.setChecked(cfg["ublock"])
        self.sync = QCheckBox("Synchronizuj z serwerem"); self.sync.setChecked(cfg["sync"])
        self.regen = QCheckBox("Wygeneruj nowy fingerprint przy zapisie")
        self.notes = QPlainTextEdit(cfg["notes"]); self.notes.setFixedHeight(60)
        self.addons = QListWidget(); self.addons.setFixedHeight(100)
        self.extra_addons: list[str] = []
        f = QFormLayout(self)
        for label, w in (("Nazwa", self.name), ("Silnik", self.engine), ("System w fingerprincie", self.os),
                         ("Język", self.locale), ("Strefa czasowa", self.tz), ("Strona startowa", self.url),
                         ("Rozszerzenia", self.addons), ("", self.webrtc), ("", self.human), ("", self.ubo),
                         ("", self.sync), ("Notatki", self.notes)):
            f.addRow(label, w)
        if meta:
            f.addRow("", self.regen)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.engine.currentIndexChanged.connect(self.on_engine)
        self.on_engine()

    def on_engine(self):
        eng = self.engine.currentData()
        self.os.setEnabled(eng == "firefox")      # w silniku Chrome platforma wynika z systemu hosta
        self.ubo.setEnabled(eng == "firefox")
        self.human.setEnabled(eng == "firefox")
        selected = set(self.cfg0["addons"]) if (self.existing and eng == self.cfg0["engine"]) else set()
        self.addons.clear()
        lib = addons.installed(eng)
        for a in lib:
            it = QListWidgetItem(f"{a['name']} [{a['slug']}]")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if a["slug"] in selected else Qt.Unchecked)
            it.setData(Qt.UserRole, a["slug"]); self.addons.addItem(it)
        known = {a["slug"] for a in lib}
        self.extra_addons = [s for s in selected if s not in known]   # np. profil z drugiego komputera

    def result_config(self) -> tuple[str, dict, bool]:
        slugs = [self.addons.item(i).data(Qt.UserRole) for i in range(self.addons.count())
                 if self.addons.item(i).checkState() == Qt.Checked] + self.extra_addons
        cfg = {"engine": self.engine.currentData(), "os": self.os.currentText(), "locale": self.locale.text().strip(),
               "timezone": self.tz.text().strip(), "start_url": self.url.text().strip() or "about:blank",
               "block_webrtc": self.webrtc.isChecked(), "humanize": self.human.isChecked(),
               "ublock": self.ubo.isChecked(), "fp_seed": self.cfg0.get("fp_seed", 0), "addons": slugs, "sync": self.sync.isChecked(),
               "notes": self.notes.toPlainText()}
        regen = self.regen.isChecked() or (self.orig_os is not None and cfg["engine"] == "firefox" and cfg["os"] != self.orig_os)
        return self.name.text().strip() or "Profil", cfg, regen


# ----------------------------------------------------------------- okno główne
class MainWindow(QMainWindow):
    COLS = ["Nazwa", "Silnik", "Rozszerzenia", "Sync", "Status"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Profile Manager")
        self.resize(980, 480)
        self.s = core.load_settings()
        self.api = Api(self.s)
        self.server: dict[str, dict] = {}
        self.online = False
        self.running: dict[str, dict] = {}       # przeglądarka działa: id -> {proc, last_hb}
        self.busy: set[str] = set()              # operacje, w trakcie których nie wolno uruchamiać (przygotowanie, pakowanie)
        self.uploading: set[str] = set()         # trwa wysyłka (wejść do profilu MOŻNA)
        self.next_snap: dict[str, Path] = {}     # najnowsza migawka czekająca na koniec trwającej wysyłki
        self.pending: dict[str, str] = {}        # wysyłka się nie udała (wejść MOŻNA, ponowienie: „Synchronizuj teraz”)
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
        for buttons in ((("Nowy", self.new), ("Uruchom", self.launch), ("Edytuj", self.edit), ("Usuń", self.delete),
                         ("Synchronizuj teraz", self.sync_now)),
                        (("Rozszerzenia", self.open_addons), ("Ustawienia", self.open_settings),
                         ("2FA / konto", self.open_security), ("Odśwież", self.refresh),
                         ("Zaloguj / zmień serwer", self.login))):
            row = QHBoxLayout()
            for text, fn in buttons:
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
        return core.load_meta(self.table.item(r, 0).data(Qt.UserRole)) if r >= 0 else None

    def has_token(self) -> bool:
        return bool(self.s.get("token") and self.s.get("server_url"))

    def active(self, pid) -> bool:
        return pid in self.running or pid in self.busy or pid in self.uploading or pid in self.pending or pid in self.next_snap

    # ---- start
    def startup(self):
        self.run(ensure_camoufox, done=lambda _: self.set_ready(), title="Przygotowuję przeglądarkę…",
                 failed=lambda e: (self.set_ready(), QMessageBox.warning(self, "Camoufox", f"Nie udało się pobrać przeglądarki:\n{e}\n\nPonowię próbę przy uruchomieniu profilu Firefox.")))
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
            self.online = False; self.fill(); return
        def ok(rows):
            self.online = True
            self.server = {r["id"]: r for r in rows}
            for r in rows:
                m = core.load_meta(r["id"])
                if m is None:
                    core.save_meta({"id": r["id"], "name": r["name"], "config": r["config"], "revision": 0, "dirty": False})
                elif not self.active(r["id"]):
                    core.update_meta(r["id"], name=r["name"], config=r["config"])
            self.fill()
        def bad(e):
            self.online = False
            self.statusBar().showMessage("Tryb offline: " + err_text(e))
            if isinstance(e, ApiError) and e.status == 401:
                self.s["token"] = ""; core.save_settings(self.s)
            self.fill()
        self.run(lambda _: self.api.list_profiles(), ok, bad, modal=False)

    def status_text(self, m: dict, cfg: dict) -> str:
        pid, srv = m["id"], self.server.get(m["id"])
        if pid in self.running:
            return "▶ uruchomiony" + (" · wysyłka poprzedniej sesji…" if pid in self.uploading else "")
        if pid in self.busy:
            return "… trwa operacja"
        if pid in self.uploading or pid in self.next_snap:
            return "⬆ wysyłanie na serwer (można wejść do profilu)"
        if pid in self.pending:
            return "⚠ błąd wysyłki (można wejść): " + self.pending[pid]
        if cfg["sync"] and self.online and srv is None:
            return "brak na serwerze (zapisz profil, by wysłać)"
        if srv and srv["lock"]["active"] and srv["lock"]["device_id"] != self.s["device_id"]:
            return f"🔒 używany na: {srv['lock']['device_name']}"
        if cfg["sync"] and srv and srv["revision"] != m["revision"]:
            return "⬇ nowsza wersja na serwerze"
        return "✓ gotowy"

    def fill(self):
        keep = self.table.currentRow()
        metas = core.list_local()
        self.table.setRowCount(len(metas))
        for i, m in enumerate(metas):
            cfg = core.merged_config(m)
            sync_txt = ("rew. %d" % m["revision"]) if cfg["sync"] else "tylko lokalnie"
            vals = [m["name"], core.ENGINES.get(cfg["engine"], cfg["engine"]), ", ".join(cfg["addons"]) or "—",
                    sync_txt, self.status_text(m, cfg)]
            for j, v in enumerate(vals):
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
            pr("Tworzę profil (dla Firefoksa: generuję fingerprint, za pierwszym razem pobiera model)…", -1)
            meta = core.create_local(name, cfg)
            if cfg["sync"]:
                try:
                    self.api.create_profile(meta["id"], name, meta["config"], core.read_fp_encoded(meta["id"]))
                except Exception:
                    core.delete_local(meta["id"]); raise
            return meta
        self.run(job, lambda _: self.refresh(), title="Tworzę profil…")

    def edit(self):
        meta = self.selected()
        if not meta or self.active(meta["id"]):
            return
        d = ProfileDialog(self, meta)
        if not d.exec():
            return
        name, cfg, regen = d.result_config()
        pid = meta["id"]
        def job(pr):
            fp = None
            if regen and cfg["engine"] == "firefox":
                pr("Generuję nowy fingerprint…", -1)
                fp = core.encode_fp(core.generate_fp(cfg["os"]))
            if regen and cfg["engine"] == "chrome":     # nowy seed = nowy fingerprint (trafia do configu i na serwer)
                cfg["fp_seed"] = random.randint(100000, 2**31 - 1)
            if cfg["sync"] and self.has_token():
                if pid in self.server:
                    kw = {"name": name, "config": cfg}
                    if fp: kw["fingerprint"] = fp
                    self.api.update_profile(pid, **kw)
                else:
                    self.api.create_profile(pid, name, cfg, fp or core.read_fp_encoded(pid))
            if fp:
                core.write_fp_encoded(pid, fp)
            core.update_meta(pid, name=name, config=cfg)
        self.run(job, lambda _: self.refresh(), title="Zapisuję…")

    def delete(self):
        meta = self.selected()
        if not meta or self.active(meta["id"]):
            return
        if QMessageBox.question(self, "Usuń", f"Usunąć profil „{meta['name']}” lokalnie i na serwerze?\nTego nie da się cofnąć.") != QMessageBox.Yes:
            return
        def job(_):
            if meta["id"] in self.server and self.has_token():
                self.api.delete_profile(meta["id"])
            core.delete_local(meta["id"])
        self.run(job, lambda _: self.refresh())

    def open_addons(self): AddonsDialog(self).exec()

    def open_settings(self): SettingsDialog(self, self.s).exec()

    def open_security(self):
        SecurityDialog(self, self.api).exec() if self.has_token() else self.login()

    # ---- uruchamianie
    def launch(self):
        meta = self.selected()
        if not meta:
            return
        pid, cfg = meta["id"], core.merged_config(meta)
        if pid in self.running or pid in self.busy:
            return
        local_mode = pid in self.uploading or pid in self.pending or pid in self.next_snap
        if cfg["engine"] == "firefox" and not self.browser_ready:
            return QMessageBox.information(self, "Profile Manager", "Przeglądarka jeszcze się pobiera, poczekaj chwilę.")
        if cfg["sync"] and not local_mode and not self.has_token():
            return QMessageBox.information(self, "Sync", "Ten profil jest synchronizowany. Zaloguj się, żeby go uruchomić.")
        self.busy.add(pid); self.pending.pop(pid, None); self.fill()
        def job(pr):
            addons.ensure(cfg["addons"], cfg["engine"], pr)
            if cfg["engine"] == "chrome":
                ensure_chrome_engine(pr)
            else:
                ensure_camoufox(pr)
            if local_mode:                     # lokalny katalog jest najnowszy, blokada nadal nasza
                sync.prepare_local(pid)
                return ""
            return sync.prepare(self.api, pid, pr)
        def ok(warn):
            self.busy.discard(pid)
            if warn:
                QMessageBox.information(self, "Uwaga", warn)
            cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")]
            log = open(core.LOGS / f"{pid}.log", "ab")
            proc = subprocess.Popen(cmd + ["--worker", pid], stdout=log, stderr=log,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.running[pid] = {"proc": proc, "last_hb": time.time(), "t0": time.time()}
            self.fill()
        def bad(e):
            self.busy.discard(pid); self.fill(); self.fail(e); self.refresh()
        self.run(job, ok, bad, title="Przygotowuję profil…")

    def tick(self):
        now = time.time()
        for pid, r in list(self.running.items()):
            rc = r["proc"].poll()
            if rc is not None:
                del self.running[pid]
                if rc != 0:
                    QTimer.singleShot(0, lambda p=pid: self.show_crash(p))
                self.session_ended(pid)
            elif now - r["last_hb"] > 30:
                r["last_hb"] = now
                self.heartbeat(pid)
        for pid in (set(self.pending) | self.uploading | set(self.next_snap) | self.busy) - set(self.running):
            if now - self.pending_hb.get(pid, 0) > 30:
                self.pending_hb[pid] = now
                self.heartbeat(pid)

    def show_crash(self, pid):
        meta = core.load_meta(pid) or {}
        try:
            tail = "\n".join((core.LOGS / f"{pid}.log").read_text("utf-8", "replace").splitlines()[-12:])
        except Exception:
            tail = ""
        QMessageBox.warning(self, "Przeglądarka zakończyła się błędem", f"Ostatnie linie logu:\n\n{tail}")

    def heartbeat(self, pid):
        m = core.load_meta(pid)
        if not m or not core.merged_config(m)["sync"] or not self.has_token():
            return
        t = Task(lambda _: self.api.heartbeat(pid))
        t.failed.connect(lambda e, t=t: (self._tasks.remove(t), self.statusBar().showMessage("Heartbeat: " + err_text(e))))
        t.done.connect(lambda _, t=t: self._tasks.remove(t))
        self._tasks.append(t); t.start()

    # ---- koniec sesji -> migawka -> wysyłka w tle (profil można od razu uruchomić ponownie)
    def session_ended(self, pid):
        meta = core.load_meta(pid)
        if not meta or not core.merged_config(meta)["sync"]:
            core.update_meta(pid, dirty=False); self.fill(); return
        self.snapshot_and_enqueue(pid)

    def snapshot_and_enqueue(self, pid):
        self.busy.add(pid); self.pending.pop(pid, None); self.fill()
        def ok(arc):
            self.busy.discard(pid)
            self.enqueue_upload(pid, arc)
        def bad(e):
            self.busy.discard(pid); self.pending[pid] = err_text(e); self.fill()
        self.run(lambda pr: sync.snapshot(pid, pr), ok, bad, modal=False)

    def enqueue_upload(self, pid, arc: Path):
        old = self.next_snap.pop(pid, None)
        if old:
            old.unlink(missing_ok=True)      # nowsza migawka zawiera wszystko, co starsza
        if pid in self.uploading:
            self.next_snap[pid] = arc
        else:
            self.start_upload(pid, arc)
        self.fill()

    def start_upload(self, pid, arc: Path):
        self.uploading.add(pid); self.pending.pop(pid, None); self.fill()
        def ok(_):
            self.uploading.discard(pid)
            nxt = self.next_snap.pop(pid, None)
            if nxt:
                self.start_upload(pid, nxt)
            elif pid not in self.running and pid not in self.busy:
                self.run(lambda pr: sync.release(self.api, pid), lambda _: self.after_sync(pid), modal=False)
            else:
                self.after_sync(pid)           # profil znów działa: blokadę zwolni następna sesja
        def bad(e):
            self.uploading.discard(pid)
            for f in (self.next_snap.pop(pid, None),):
                if f: f.unlink(missing_ok=True)
            self.pending[pid] = err_text(e)
            self.fill()
            self.statusBar().showMessage("Nie udało się wysłać profilu. „Synchronizuj teraz” ponowi wysyłkę.")
        self.run(lambda pr: sync.upload_snapshot(self.api, pid, arc, pr), ok, bad, modal=False)

    def after_sync(self, pid):
        self.statusBar().showMessage("Profil zsynchronizowany.")
        self.refresh()

    def sync_now(self):
        meta = self.selected()
        if not meta:
            return
        pid = meta["id"]
        if pid in self.pending and pid not in self.running and pid not in self.busy:
            self.snapshot_and_enqueue(pid)
        else:
            self.statusBar().showMessage("Ten profil nie czeka na ręczną synchronizację.")

    def closeEvent(self, ev):
        if any(self.active(p) for p in list(self.running) + list(self.busy) + list(self.uploading) + list(self.pending)):
            QMessageBox.warning(self, "Profile Manager", "Zamknij najpierw przeglądarki i poczekaj na zakończenie synchronizacji.")
            ev.ignore()
        else:
            ev.accept()


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
