# Profile Manager

Anty-detekt „multi-account browser": profile z osobnymi cookies, maskowaniem fingerprintu (fingerprint-chromium),
rozszerzeniami, zakładkami i synchronizacją przez własny serwer.

- **Klient (Windows, Python/PySide6 → .exe)**: przeglądarka działa **lokalnie z dysku**. Na serwer idzie tylko spakowany profil
  przy zamknięciu przeglądarki, a pobierany jest przy otwarciu, jeśli na serwerze jest nowsza wersja. Żadnego streamowania.
- **Serwer (Linux, Docker: FastAPI + PostgreSQL)**: konta, 2FA (TOTP + kody zapasowe), blokada profilu
  (jeden komputer naraz, wygasa po 90 s bez heartbeatu), wersjonowanie, szyfrowanie blobów po stronie serwera.

## Serwer
```bash
cp .env.example .env
docker compose run --rm app python -m app.cli gen-keys   # wklej SECRET_KEY i ENCRYPTION_KEY do .env, ustaw POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec app python -m app.cli create-user TWOJ_LOGIN --admin
```
Dostęp spoza domu: **nie wystawiaj gołego HTTP do internetu**. Użyj reverse proxy z HTTPS (Caddy/nginx), Cloudflare Tunnel albo Tailscale/WireGuard.

**Publiczne repo bez ryzyka, że ktoś używa Twojego serwera:** sekrety są tylko w `.env` (jest w `.gitignore`).
`REGISTRATION_MODE=closed` (domyślnie) = nikt nie założy konta, tylko Ty przez CLI. Inne tryby: `invite` (kody:
`python -m app.cli create-invite`) i `open`. Do tego limity: `MAX_USERS`, `MAX_PROFILE_MB`, `USER_QUOTA_MB`, `MAX_PROFILES_PER_USER`.
Każdy, kto sklonuje repo, stawia własny serwer z własnymi kluczami.

Komendy CLI: `create-user`, `create-invite`, `list-users`, `reset-2fa USER`, `delete-user USER`.

## Klient
Wymagany Python 3.12+ (Windows).
```bat
cd client
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py                 :: tryb deweloperski
build.bat                      :: buduje dist\ProfileManager\ProfileManager.exe (PyInstaller, --onedir)
```
Silnik Chrome (fingerprint-chromium) pobiera się jednorazowo przy pierwszym uruchomieniu profilu albo w „Ustawienia".
Dane lokalne: `%APPDATA%\ProfileManager\` (profile, rozszerzenia, logi przeglądarek w `logs\`).

W „Rozszerzenia" biblioteka jest początkowo pusta (nic nie jest preinstalowane). Rozszerzenia dodajesz po ID/linku z Chrome Web Store
albo z pliku (.crx/.zip) lub rozpakowanego folderu, a przypisujesz je do profili w „Edytuj". Brakujące rozszerzenia doinstalują się same na drugim komputerze.

## Interfejs
Profile są wyświetlane jako kafelki. Każdy kafelek ma własne przyciski: **Start**, **Force stop** (aktywny tylko, gdy profil działa; zabija przeglądarkę
razem z całym drzewem procesów, po czym ostatni stan profilu jest synchronizowany i blokada zwalniana), **Edytuj**, **Usuń** oraz **Synchronizuj**
(gdy wysyłka się nie udała). Dwuklik na kafelku też uruchamia profil. Na dole okna widać status połączenia i wersję aplikacji (`pm/__init__.py`).

## Strona startowa i zakładki
- **Strona startowa** może być wpisana bez `https://` (np. `example.com`); adresy lokalne (`localhost`, IP) dostają `http://`.
  Jeśli strona się nie załaduje (brak sieci, zły proxy), przeglądarka zostaje otwarta i pokazuje stronę błędu.
- **Zakładki**: w „Nowy/Edytuj profil" pole „Zakładki", jedna na linię: `Tytuł | adres` albo sam adres (linie z `#` są pomijane).
  Przy starcie profilu trafiają na pasek zakładek (i pasek zostaje włączony na wszystkich kartach). Każda zakładka dodaje się **raz**:
  usunięta przez Ciebie nie wraca, nowa wpisana w polu dodaje się przy następnym uruchomieniu, a Twoje własne zakładki i duplikaty adresów są pomijane.
  W „Ustawienia" jest lista **domyślnych zakładek**, która wypełnia pole przy tworzeniu nowego profilu.

## Silnik: Chrome (fingerprint-chromium)
Otwartoźródłowy Chromium z maskowaniem fingerprintu na poziomie binarki (BSD-3), bez kluczy i **bez limitu jednoczesnych sesji**.
Seed fingerprintu leży w configu profilu, więc profil wygląda tak samo na obu komputerach. Rozszerzenia pochodzą z Chrome Web Store (CRX,
ID zachowane, więc ich dane przenoszą się między komputerami). Chrome na Windows szyfruje cookies kluczem komputera, więc aplikacja zapisuje
ich kopię w profilu (`pm-cookies.json`, co ~8 s) i wczytuje przy starcie. Silnik pobiera się z GitHub Releases (adryfish/fingerprint-chromium)
przy pierwszym uruchomieniu profilu albo w „Ustawienia"; własną binarkę możesz wskazać zmienną `PM_CHROMIUM_EXE`.

Silnik Firefox (Camoufox) został usunięty. Stare profile Firefox nadal są na liście, ale można je tylko usunąć.

## Wejście do profilu w trakcie wysyłki
Po zamknięciu przeglądarki profil jest „zamrażany" do archiwum (kilka sekund), a wysyłka dzieje się w tle. W tym czasie możesz od razu
uruchomić profil ponownie (blokada nadal należy do tego urządzenia, a lokalny katalog jest najnowszy). Kolejne migawki układają się w łańcuch
(zawsze wysyłana jest najnowsza), a blokada jest zwalniana dopiero po ostatniej sesji. Jeśli wysyłka się nie uda, profil nadal można
uruchomić, a „Synchronizuj teraz" ponawia ją.

## Jak działa sync
1. Uruchom → serwer zakłada blokadę (409 + nazwa urządzenia, jeśli profil jest zajęty) → pobranie, jeśli serwer ma nowszą rewizję.
2. Przeglądarka działa lokalnie, a heartbeat co 30 s podtrzymuje blokadę.
3. Zamknięcie przeglądarki → pakowanie (bez cache) → wysyłka z kontrolą rewizji → zwolnienie blokady.
4. Jeśli wysyłka się nie uda (brak sieci), blokada zostaje, a profil czeka na ponowienie (przycisk Uruchom na nim).
   Lokalne zmiany nie giną. Przy rozjazdie wersji lokalna kopia trafia do `data.conflict-*`.

## Testy
`python tests/e2e_smoke.py`, `python tests/test_crx.py` i `python tests/test_bookmarks.py` (serwer na SQLite + dwa symulowane urządzenia: 2FA, blokady, wysyłka, pobranie, konflikty, szyfrowanie).

## Ograniczenia
- Fingerprint (seed) tworzony jest raz na profil i zapisywany (jest taki sam na obu komputerach). „Wygeneruj nowy fingerprint przy zapisie" losuje nowy seed.
- Token logowania leży w `settings.json` w `%APPDATA%`.
- Szyfrowanie po stronie serwera chroni dane „w spoczynku" (dysk/backup), ale administrator serwera z kluczem może je odszyfrować.
