import hashlib
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import __version__, addons, bookmarks, chromium, core, sync
from .api import Api, ApiError

LEGACY_MSG = ("Ten profil używa silnika Firefox (Camoufox), który został usunięty z aplikacji.\n"
              "Można go tylko usunąć. Utwórz nowy profil (Chrome).")

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


PRESET_KEYS = ("locale", "timezone", "start_url", "block_webrtc", "addons", "bookmarks", "sync", "notes")


def preset_cfg(cfg: dict) -> dict:
    """Z configu profilu zostawia tylko to, co ma sens we wzorcu (bez seeda fingerprintu i silnika)."""
    return {k: (list(cfg[k]) if isinstance(cfg[k], list) else cfg[k]) for k in PRESET_KEYS if k in cfg}


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
        self.list = QListWidget(); lay.addWidget(self.list)
        row2 = QHBoxLayout()
        for text, fn in (("Zainstaluj ze sluga / ID / linku…", self.install_custom),
                         ("Aktualizuj wszystkie", self.update_all), ("Usuń zaznaczone", self.remove)):
            b = QPushButton(text); b.clicked.connect(fn); row2.addWidget(b)
        lay.addLayout(row2)
        row3 = QHBoxLayout()
        for text, fn in (("Dodaj z pliku (.crx / .zip)…", self.add_file), ("Dodaj z folderu…", self.add_dir)):
            b = QPushButton(text); b.clicked.connect(fn); row3.addWidget(b)
        lay.addLayout(row3)
        hint = QLabel("Gdy pobieranie ze sklepu nie działa: zainstaluj rozszerzenie w zwykłym Chrome i wskaż jego folder "
                      "(…\\Chrome\\User Data\\Default\\Extensions\\<id>\\<wersja>).")
        hint.setWordWrap(True); lay.addWidget(hint)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for a in addons.installed():
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
        self.run(lambda pr: addons.install(ref, pr))

    def install_custom(self):
        text, ok = QInputDialog.getText(self, "Instalacja", "ID lub link z Chrome Web Store:")
        if ok and text.strip():
            self.install(text)

    def add_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "Wybierz rozszerzenie", "", "Rozszerzenia (*.crx *.zip);;Wszystkie pliki (*)")
        if f:
            self.run(lambda pr: addons.install_from_file(f, pr))

    def add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Wybierz folder z rozpakowanym rozszerzeniem (z manifest.json)")
        if d:
            self.run(lambda pr: addons.install_from_file(d, pr))

    def update_all(self):
        slugs = [a["slug"] for a in addons.installed()]
        self.run(lambda pr: [addons.install(s, pr) for s in slugs])

    def remove(self):
        for it in self.list.selectedItems():
            addons.remove(it.data(Qt.UserRole))
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
        lay.addWidget(QLabel("<b>Domyślne zakładki dla nowych profili</b><br>Jedna na linię: <i>Tytuł | adres</i> albo sam adres."))
        self.bm = QPlainTextEdit(self.s.get("default_bookmarks", "")); self.bm.setFixedHeight(110)
        self.bm.setPlaceholderText("Steam | https://steamcommunity.com\nhttps://example.com")
        lay.addWidget(self.bm)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        bb.accepted.connect(self.save); bb.rejected.connect(self.reject); lay.addWidget(bb)
        self.refresh()

    def save(self):
        self.s["default_bookmarks"] = bookmarks.to_text(bookmarks.parse_text(self.bm.toPlainText()))
        core.save_settings(self.s)
        self.accept()

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
    def __init__(self, parent, meta: dict | None, default_bookmarks: str = "", presets: list | None = None,
                 on_save_preset=None, preset_mode: bool = False):
        super().__init__(parent)
        self.preset_mode, self.presets, self.on_save_preset = preset_mode, presets or [], on_save_preset
        if preset_mode:
            self.setWindowTitle("Edycja presetu" if meta else "Nowy preset")
        else:
            self.setWindowTitle("Edycja profilu" if meta else "Nowy profil")
        self.setMinimumWidth(480)
        cfg = core.merged_config(meta) if meta else dict(core.DEFAULT_CONFIG)
        self.existing, self.cfg0 = meta is not None, cfg
        self.name = QLineEdit(meta["name"] if meta else "")
        self.locale = QLineEdit(cfg["locale"]); self.locale.setPlaceholderText("np. pl-PL, en-US (puste = domyślny)")
        self.tz = QLineEdit(cfg["timezone"]); self.tz.setPlaceholderText("np. Europe/Warsaw (puste = z systemu)")
        self.url = QLineEdit(cfg["start_url"])
        self.webrtc = QCheckBox("Zablokuj WebRTC (wyciek IP)"); self.webrtc.setChecked(cfg["block_webrtc"])
        self.sync = QCheckBox("Synchronizuj z serwerem"); self.sync.setChecked(cfg["sync"])
        self.regen = QCheckBox("Wygeneruj nowy fingerprint przy zapisie")
        self.notes = QPlainTextEdit(cfg["notes"]); self.notes.setFixedHeight(60)
        self.bookmarks = QPlainTextEdit(bookmarks.to_text(cfg["bookmarks"]) if meta else default_bookmarks)
        self.bookmarks.setFixedHeight(80)
        self.bookmarks.setPlaceholderText("Tytuł | https://adres  (jedna zakładka na linię; trafią na pasek zakładek przy starcie)")
        self.addons = QListWidget(); self.addons.setFixedHeight(100)
        f = QFormLayout(self)
        if not preset_mode:
            self.preset_box = QComboBox()
            self.preset_box.activated.connect(self.apply_preset)
            f.addRow("Preset", self.preset_box)
            self.fill_presets()
        for label, w in (("Nazwa presetu" if preset_mode else "Nazwa", self.name), ("Język", self.locale), ("Strefa czasowa", self.tz),
                         ("Strona startowa", self.url), ("Zakładki", self.bookmarks), ("Rozszerzenia", self.addons),
                         ("", self.webrtc), ("", self.sync), ("Notatki", self.notes)):
            f.addRow(label, w)
        if meta and not preset_mode:
            f.addRow("", self.regen)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        if not preset_mode:
            b_preset = bb.addButton("Zapisz jako preset…", QDialogButtonBox.ActionRole)
            b_preset.clicked.connect(self.save_as_preset)
        f.addRow(bb)
        self.fill_addons()

    # ---- presety
    def fill_presets(self):
        self.preset_box.clear()
        self.preset_box.addItem("— bez presetu —", None)
        for p in self.presets:
            self.preset_box.addItem(p["name"], p["id"])

    def apply_preset(self, index: int):
        pid = self.preset_box.itemData(index)
        p = next((x for x in self.presets if x["id"] == pid), None)
        if not p:
            return
        c = {**core.DEFAULT_CONFIG, **p["config"]}       # nazwy profilu nie ruszamy
        self.locale.setText(c["locale"]); self.tz.setText(c["timezone"]); self.url.setText(c["start_url"])
        self.webrtc.setChecked(c["block_webrtc"]); self.sync.setChecked(c["sync"])
        self.notes.setPlainText(c["notes"]); self.bookmarks.setPlainText(bookmarks.to_text(c["bookmarks"]))
        self.cfg0 = {**self.cfg0, "addons": list(c["addons"])}
        self.fill_addons()

    def save_as_preset(self):
        name, ok = QInputDialog.getText(self, "Nowy preset", "Nazwa presetu:", text=self.name.text())
        name = name.strip()
        if not ok or not name or not self.on_save_preset:
            return
        if any(p["name"].lower() == name.lower() for p in self.presets):
            if QMessageBox.question(self, "Preset", f"Preset „{name}” już istnieje. Nadpisać?") != QMessageBox.Yes:
                return
        _, cfg, _ = self.result_config()
        self.presets = self.on_save_preset(name, preset_cfg(cfg))
        self.fill_presets()
        self.preset_box.setCurrentIndex(max(0, self.preset_box.findText(name)))

    def fill_addons(self):
        selected = set(self.cfg0["addons"])
        self.addons.clear()
        lib = addons.installed()
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
        cfg = {"engine": "chrome", "locale": self.locale.text().strip(),
               "timezone": self.tz.text().strip(), "start_url": core.normalize_url(self.url.text()) or "about:blank",
               "block_webrtc": self.webrtc.isChecked(), "fp_seed": self.cfg0.get("fp_seed", 0), "addons": slugs,
               "bookmarks": bookmarks.parse_text(self.bookmarks.toPlainText()), "sync": self.sync.isChecked(),
               "notes": self.notes.toPlainText()}
        regen = self.regen.isChecked()
        return self.name.text().strip() or "Profil", cfg, regen


# ----------------------------------------------------------------- presety
class PresetsDialog(QDialog):
    """Zarządzanie presetami: wzorcami (zakładki, rozszerzenia, strona startowa…) do tworzenia nowych profili."""

    def __init__(self, parent, win: "MainWindow"):
        super().__init__(parent)
        self.win = win
        self.setWindowTitle("Presety profili")
        self.setMinimumSize(520, 380)
        lay = QVBoxLayout(self)
        intro = QLabel("Preset to gotowy zestaw ustawień (strona startowa, zakładki, rozszerzenia, język…), "
                       "który wybierasz przy tworzeniu lub edycji profilu.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.list = QListWidget(); self.list.itemDoubleClicked.connect(lambda _: self.edit())
        lay.addWidget(self.list)
        row = QHBoxLayout()
        for text, fn in (("Nowy", self.new), ("Edytuj", self.edit), ("Duplikuj", self.duplicate), ("Usuń", self.remove)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        lay.addLayout(row)
        self.refresh()

    @property
    def presets(self) -> list:
        return self.win.presets_list()

    def refresh(self):
        self.list.clear()
        for p in self.presets:
            c = {**core.DEFAULT_CONFIG, **p["config"]}
            start = c["start_url"] if c["start_url"] != "about:blank" else "—"
            it = QListWidgetItem(f"{p['name']}   ·   start: {start}   ·   rozszerzenia: {len(c['addons'])}   ·   zakładki: {len(c['bookmarks'])}")
            it.setData(Qt.UserRole, p["id"]); self.list.addItem(it)

    def current(self) -> dict | None:
        it = self.list.currentItem()
        return next((p for p in self.presets if it and p["id"] == it.data(Qt.UserRole)), None)

    def new(self):
        d = ProfileDialog(self, None, self.win.s.get("default_bookmarks", ""), preset_mode=True)
        if d.exec():
            name, cfg, _ = d.result_config()
            self.win.save_preset(name, preset_cfg(cfg)); self.refresh()

    def edit(self):
        p = self.current()
        if not p:
            return
        d = ProfileDialog(self, {"name": p["name"], "config": p["config"]}, preset_mode=True)
        if d.exec():
            name, cfg, _ = d.result_config()
            p["name"], p["config"] = name, preset_cfg(cfg)
            core.save_settings(self.win.s); self.refresh()

    def duplicate(self):
        p = self.current()
        if p:
            self.win.presets_list().append({"id": str(uuid.uuid4()), "name": p["name"] + " (kopia)",
                                            "config": preset_cfg(p["config"])})
            core.save_settings(self.win.s); self.refresh()

    def remove(self):
        p = self.current()
        if p and QMessageBox.question(self, "Usuń", f"Usunąć preset „{p['name']}”?\nProfile utworzone z presetu zostają bez zmian.") == QMessageBox.Yes:
            self.presets.remove(p)
            core.save_settings(self.win.s); self.refresh()


# ----------------------------------------------------------------- styl
STYLE = """
QWidget { background: #0d1117; color: #e6edf3; font-size: 13px; }
QLabel { background: transparent; }
QDialog, QMessageBox, QProgressDialog { background: #0d1117; }
QLineEdit, QPlainTextEdit, QListWidget {
    background: #010409; border: 1px solid #30363d; border-radius: 6px; padding: 5px;
    selection-background-color: #1f6feb; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #58a6ff; }
QPushButton { background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #30363d; }
QPushButton:disabled { color: #6e7681; background: #161b22; border-color: #21262d; }
QPushButton#primary { background: #238636; border-color: #2ea043; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #2ea043; }
QPushButton#primary:disabled { background: #16301d; color: #6e7681; border-color: #16301d; }
QPushButton#danger { background: #2d1417; border-color: #6e2128; color: #ff7b72; font-weight: 600; }
QPushButton#danger:hover { background: #da3633; color: white; }
QPushButton#danger:disabled { background: #161b22; color: #484f58; border-color: #21262d; }
QPushButton#small { padding: 4px 8px; background: transparent; border-color: #30363d; color: #8b949e; }
QPushButton#small:hover { background: #21262d; color: #e6edf3; }
QPushButton#small:disabled { color: #484f58; border-color: #21262d; }
QLabel#title { font-size: 22px; font-weight: 700; }
QLabel#conn { color: #8b949e; padding: 0 10px; }
QFrame#Tile { background: #161b22; border: 1px solid #30363d; border-radius: 12px; }
QFrame#Tile:hover { border-color: #58a6ff; }
QFrame#NewTile { background: transparent; border: 2px dashed #30363d; border-radius: 12px; }
QFrame#NewTile:hover { border-color: #58a6ff; background: #11161d; }
QLabel#tname { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #8b949e; font-size: 12px; }
QScrollArea, QWidget#gridbox { background: transparent; border: none; }
QStatusBar { background: #010409; border-top: 1px solid #21262d; color: #8b949e; }
QStatusBar::item { border: none; }
QLabel#version { color: #8b949e; padding: 0 10px; font-weight: 600; }
QToolTip { background: #161b22; color: #e6edf3; border: 1px solid #30363d; padding: 4px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #30363d; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

TILE_W, TILE_H, GAP = 290, 224, 16

KIND_COLOR = {"running": "#3fb950", "busy": "#d29922", "upload": "#58a6ff", "error": "#f85149",
              "locked": "#db6d28", "update": "#58a6ff", "ready": "#8b949e", "new": "#d29922"}
AVATARS = ["#1f6feb", "#8957e5", "#238636", "#db6d28", "#bf3989", "#0e8a9c", "#9e6a03", "#da3633"]


def short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def kill_tree(proc: subprocess.Popen):
    """Zabija proces workera razem z przeglądarką i sterownikiem (całe drzewo procesów)."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa
        pass
    try:
        proc.kill()
    except Exception:  # noqa
        pass


# ----------------------------------------------------------------- kafelki
class Tile(QFrame):
    """Kafelek profilu z przyciskami Start / Force stop / Edytuj / Usuń (/ Sync)."""

    def __init__(self, win: "MainWindow", m: dict, cfg: dict, kind: str, status: str):
        super().__init__()
        self.setObjectName("Tile")
        self.setFixedSize(TILE_W, TILE_H)
        self.win, self.pid = win, m["id"]
        pid = self.pid
        running = pid in win.running
        active = win.active(pid)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)

        head = QHBoxLayout(); head.setSpacing(12)
        av = QLabel((m["name"].strip()[:1] or "?").upper())
        av.setFixedSize(42, 42); av.setAlignment(Qt.AlignCenter)
        color = AVATARS[int(hashlib.md5(pid.encode()).hexdigest()[:6], 16) % len(AVATARS)]
        av.setStyleSheet(f"background:{color}; color:white; border-radius:21px; font-size:19px; font-weight:700;")
        head.addWidget(av)
        col = QVBoxLayout(); col.setSpacing(2)
        name = QLabel(short(m["name"], 24)); name.setObjectName("tname"); name.setToolTip(m["name"])
        st = QLabel(f"<span style='color:{KIND_COLOR.get(kind, '#8b949e')}'>●</span> {short(status, 46)}")
        st.setObjectName("muted"); st.setToolTip(status)
        col.addWidget(name); col.addWidget(st)
        head.addLayout(col, 1)
        lay.addLayout(head)

        sync_txt = f"rew. {m['revision']}" if cfg["sync"] else "tylko lokalnie"
        n_ext, n_bm = len(cfg["addons"]), len(cfg.get("bookmarks") or [])
        info = QLabel(f"Chrome · {sync_txt}<br>Rozszerzenia: {n_ext} · Zakładki: {n_bm}")
        info.setObjectName("muted")
        lay.addWidget(info)
        extra = cfg["start_url"] if cfg["start_url"] != "about:blank" else (cfg["notes"].splitlines() or [""])[0]
        sub = QLabel(short(extra, 40)); sub.setObjectName("muted"); sub.setToolTip(extra)
        lay.addWidget(sub)
        lay.addStretch()

        row = QHBoxLayout(); row.setSpacing(8)
        b_start = QPushButton("▶  Start"); b_start.setObjectName("primary")
        b_start.setEnabled(not running and pid not in win.busy)
        b_start.clicked.connect(lambda _=False: win.launch(pid))
        b_stop = QPushButton("■  Force stop"); b_stop.setObjectName("danger")
        b_stop.setEnabled(running)
        b_stop.clicked.connect(lambda _=False: win.force_stop(pid))
        row.addWidget(b_start); row.addWidget(b_stop)
        lay.addLayout(row)

        row2 = QHBoxLayout(); row2.setSpacing(6)
        b_edit = QPushButton("Edytuj"); b_edit.setObjectName("small"); b_edit.setEnabled(not active)
        b_edit.clicked.connect(lambda _=False: win.edit(pid))
        b_del = QPushButton("Usuń"); b_del.setObjectName("small"); b_del.setEnabled(not active)
        b_del.clicked.connect(lambda _=False: win.delete(pid))
        row2.addWidget(b_edit); row2.addWidget(b_del)
        if pid in win.pending and not running and pid not in win.busy:
            b_sync = QPushButton("Synchronizuj"); b_sync.setObjectName("small")
            b_sync.clicked.connect(lambda _=False: win.sync_now(pid))
            row2.addWidget(b_sync)
        lay.addLayout(row2)

    def mouseDoubleClickEvent(self, ev):
        if self.pid not in self.win.running and self.pid not in self.win.busy:
            self.win.launch(self.pid)


class NewTile(QFrame):
    def __init__(self, callback):
        super().__init__()
        self.setObjectName("NewTile")
        self.setFixedSize(TILE_W, TILE_H)
        self.setCursor(Qt.PointingHandCursor)
        self.callback = callback
        lay = QVBoxLayout(self)
        plus = QLabel("+"); plus.setAlignment(Qt.AlignCenter)
        plus.setStyleSheet("font-size: 44px; color: #58a6ff; font-weight: 300;")
        txt = QLabel("Nowy profil"); txt.setAlignment(Qt.AlignCenter); txt.setObjectName("muted")
        lay.addStretch(); lay.addWidget(plus); lay.addWidget(txt); lay.addStretch()

    def mousePressEvent(self, ev):
        self.callback()


class TileGrid(QScrollArea):
    """Siatka kafelków, która sama dobiera liczbę kolumn do szerokości okna."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.box = QWidget(); self.box.setObjectName("gridbox")
        self.grid = QGridLayout(self.box)
        self.grid.setSpacing(GAP)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setWidget(self.box)
        self.tiles: list[QWidget] = []
        self._cols = 0

    def set_tiles(self, tiles: list[QWidget]):
        for t in self.tiles:
            self.grid.removeWidget(t); t.hide(); t.setParent(None); t.deleteLater()
        self.tiles = tiles
        self.relayout()

    def relayout(self):
        cols = max(1, (self.viewport().width() + GAP) // (TILE_W + GAP))
        self._cols = cols
        for t in self.tiles:
            self.grid.removeWidget(t)
        for i, t in enumerate(self.tiles):
            self.grid.addWidget(t, i // cols, i % cols)
            t.show()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if max(1, (self.viewport().width() + GAP) // (TILE_W + GAP)) != self._cols:
            self.relayout()


# ----------------------------------------------------------------- okno główne
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Profile Manager {__version__}")
        self.resize(1000, 640)
        self.setMinimumSize(820, 420)
        self.s = core.load_settings()
        self.api = Api(self.s)
        self.server: dict[str, dict] = {}
        self.online = False
        self.running: dict[str, dict] = {}       # przeglądarka działa: id -> {proc, last_hb}
        self.busy: set[str] = set()              # operacje, w trakcie których nie wolno uruchamiać (przygotowanie, pakowanie)
        self.uploading: set[str] = set()         # trwa wysyłka (wejść do profilu MOŻNA)
        self.next_snap: dict[str, Path] = {}     # najnowsza migawka czekająca na koniec trwającej wysyłki
        self.pending: dict[str, str] = {}        # wysyłka się nie udała (wejść MOŻNA, ponowienie: „Synchronizuj”)
        self.pending_hb: dict[str, float] = {}
        self._tasks: list[Task] = []

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 18, 24, 12)
        root.setSpacing(16)

        head = QHBoxLayout(); head.setSpacing(8)
        title = QLabel("Profile Manager"); title.setObjectName("title")
        self.conn = QLabel(); self.conn.setObjectName("conn")
        title.setMinimumWidth(190)
        head.addWidget(title); head.addStretch()
        for text, fn, name in (("+ Nowy profil", self.new, "primary"), ("Presety", self.open_presets, ""), ("Rozszerzenia", self.open_addons, ""),
                               ("Ustawienia", self.open_settings, ""), ("Konto / 2FA", self.open_security, ""),
                               ("Odśwież", self.refresh, ""), ("Logowanie", self.login, "")):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(fn)
            head.addWidget(b)
        root.addLayout(head)

        self.grid = TileGrid()
        root.addWidget(self.grid, 1)
        self.setCentralWidget(central)

        ver = QLabel(f"Wersja {__version__}"); ver.setObjectName("version")
        self.statusBar().addPermanentWidget(self.conn)
        self.statusBar().addPermanentWidget(ver)
        self.statusBar().setSizeGripEnabled(False)
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

    def has_token(self) -> bool:
        return bool(self.s.get("token") and self.s.get("server_url"))

    def active(self, pid) -> bool:
        return pid in self.running or pid in self.busy or pid in self.uploading or pid in self.pending or pid in self.next_snap

    # ---- start
    def startup(self):
        if not self.has_token():
            self.login()
        self.refresh()

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

    def status(self, m: dict, cfg: dict) -> tuple[str, str]:
        """(rodzaj, tekst) statusu profilu; rodzaj wybiera kolor kropki na kafelku."""
        pid, srv = m["id"], self.server.get(m["id"])
        if pid in self.running:
            return "running", "uruchomiony" + (" · wysyłka poprzedniej sesji…" if pid in self.uploading else "")
        if pid in self.busy:
            return "busy", "trwa operacja…"
        if pid in self.uploading or pid in self.next_snap:
            return "upload", "wysyłanie na serwer (można wejść)"
        if pid in self.pending:
            return "error", "błąd wysyłki: " + self.pending[pid]
        if cfg["sync"] and self.online and srv is None:
            return "new", "brak na serwerze (zapisz profil)"
        if srv and srv["lock"]["active"] and srv["lock"]["device_id"] != self.s["device_id"]:
            return "locked", f"używany na: {srv['lock']['device_name']}"
        if cfg["sync"] and srv and srv["revision"] != m["revision"]:
            return "update", "nowsza wersja na serwerze"
        return "ready", "gotowy"

    def fill(self):
        if self.online:
            self.conn.setText(f"<span style='color:#3fb950'>●</span> online · {self.s.get('username') or ''}")
        else:
            self.conn.setText("<span style='color:#f85149'>●</span> offline")
        tiles: list[QWidget] = []
        for m in core.list_local():
            cfg = core.merged_config(m)
            kind, text = self.status(m, cfg)
            tiles.append(Tile(self, m, cfg, kind, text))
        tiles.append(NewTile(self.new))
        self.grid.set_tiles(tiles)

    # ---- CRUD
    def new(self):
        d = ProfileDialog(self, None, self.s.get("default_bookmarks", ""), self.presets_list(), self.save_preset)
        if not d.exec():
            return
        name, cfg, _ = d.result_config()
        if cfg["sync"] and not self.has_token():
            return QMessageBox.information(self, "Sync", "Zaloguj się, aby synchronizować profil, albo odznacz „Synchronizuj”.")
        def job(pr):
            pr("Tworzę profil…", -1)
            meta = core.create_local(name, cfg)
            if cfg["sync"]:
                try:
                    self.api.create_profile(meta["id"], name, meta["config"], core.read_fp_encoded(meta["id"]))
                except Exception:
                    core.delete_local(meta["id"]); raise
            return meta
        self.run(job, lambda _: self.refresh(), title="Tworzę profil…")

    def edit(self, pid: str):
        meta = core.load_meta(pid)
        if not meta or self.active(pid):
            return
        if core.merged_config(meta)["engine"] != "chrome":
            return QMessageBox.information(self, "Profile Manager", LEGACY_MSG)
        d = ProfileDialog(self, meta, "", self.presets_list(), self.save_preset)
        if not d.exec():
            return
        name, cfg, regen = d.result_config()
        def job(pr):
            if regen:                                   # nowy seed = nowy fingerprint (trafia do configu i na serwer)
                cfg["fp_seed"] = random.randint(100000, 2**31 - 1)
            if cfg["sync"] and self.has_token():
                if pid in self.server:
                    self.api.update_profile(pid, name=name, config=cfg)
                else:
                    self.api.create_profile(pid, name, cfg, core.read_fp_encoded(pid))
            core.update_meta(pid, name=name, config=cfg)
        self.run(job, lambda _: self.refresh(), title="Zapisuję…")

    def delete(self, pid: str):
        meta = core.load_meta(pid)
        if not meta or self.active(pid):
            return
        if QMessageBox.question(self, "Usuń", f"Usunąć profil „{meta['name']}” lokalnie i na serwerze?\nTego nie da się cofnąć.") != QMessageBox.Yes:
            return
        def job(_):
            if pid in self.server and self.has_token():
                self.api.delete_profile(pid)
            core.delete_local(pid)
        self.run(job, lambda _: self.refresh())

    def presets_list(self) -> list:
        return self.s.setdefault("presets", [])

    def save_preset(self, name: str, cfg: dict) -> list:
        """Dodaje preset albo nadpisuje istniejący o tej samej nazwie. Zwraca aktualną listę."""
        lst = self.presets_list()
        for p in lst:
            if p["name"].lower() == name.lower():
                p["name"], p["config"] = name, cfg
                break
        else:
            lst.append({"id": str(uuid.uuid4()), "name": name, "config": cfg})
        core.save_settings(self.s)
        return lst

    def open_presets(self):
        PresetsDialog(self, self).exec()

    def open_addons(self): AddonsDialog(self).exec()

    def open_settings(self): SettingsDialog(self, self.s).exec()

    def open_security(self):
        SecurityDialog(self, self.api).exec() if self.has_token() else self.login()

    # ---- uruchamianie
    def launch(self, pid: str):
        meta = core.load_meta(pid)
        if not meta:
            return
        cfg = core.merged_config(meta)
        if pid in self.running or pid in self.busy:
            return
        local_mode = pid in self.uploading or pid in self.pending or pid in self.next_snap
        if cfg["engine"] != "chrome":
            return QMessageBox.information(self, "Profile Manager", LEGACY_MSG)
        if cfg["sync"] and not local_mode and not self.has_token():
            return QMessageBox.information(self, "Sync", "Ten profil jest synchronizowany. Zaloguj się, żeby go uruchomić.")
        self.busy.add(pid); self.pending.pop(pid, None); self.fill()
        def job(pr):
            addons.ensure(cfg["addons"], pr)
            ensure_chrome_engine(pr)
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
            kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform.startswith("win") \
                else {"start_new_session": True}       # własna grupa procesów, żeby Force stop zabił też przeglądarkę
            proc = subprocess.Popen(cmd + ["--worker", pid], stdout=log, stderr=log, **kw)
            self.running[pid] = {"proc": proc, "last_hb": time.time(), "t0": time.time()}
            self.fill()
        def bad(e):
            self.busy.discard(pid); self.fill(); self.fail(e); self.refresh()
        self.run(job, ok, bad, title="Przygotowuję profil…")

    def force_stop(self, pid: str):
        r = self.running.get(pid)
        if not r:
            return
        name = (core.load_meta(pid) or {}).get("name", pid)
        if QMessageBox.question(
                self, "Force stop",
                f"Wymusić zatrzymanie profilu „{name}”?\n\n"
                "Przeglądarka zostanie zabita bez normalnego zamykania, więc ostatnie zmiany "
                "(np. świeżo zapisane cookies) mogą przepaść. Profil w ostatnim stanie zostanie "
                "zsynchronizowany z serwerem, a blokada zwolniona.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        r["forced"] = True
        kill_tree(r["proc"])
        self.statusBar().showMessage(f"Wymuszono zatrzymanie profilu „{name}”.")

    def tick(self):
        now = time.time()
        for pid, r in list(self.running.items()):
            rc = r["proc"].poll()
            if rc is not None:
                del self.running[pid]
                if rc != 0 and not r.get("forced"):
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
            self.statusBar().showMessage("Nie udało się wysłać profilu. „Synchronizuj” ponowi wysyłkę.")
        self.run(lambda pr: sync.upload_snapshot(self.api, pid, arc, pr), ok, bad, modal=False)

    def after_sync(self, pid):
        self.statusBar().showMessage("Profil zsynchronizowany.")
        self.refresh()

    def sync_now(self, pid: str):
        if pid in self.pending and pid not in self.running and pid not in self.busy:
            self.snapshot_and_enqueue(pid)
        else:
            self.statusBar().showMessage("Ten profil nie czeka na ręczną synchronizację.")

    def closeEvent(self, ev):
        if any(self.active(p) for p in list(self.running) + list(self.busy) + list(self.uploading) + list(self.pending)):
            QMessageBox.warning(self, "Profile Manager",
                                "Zamknij najpierw przeglądarki (albo użyj Force stop) i poczekaj na zakończenie synchronizacji.")
            ev.ignore()
        else:
            ev.accept()


def run():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
