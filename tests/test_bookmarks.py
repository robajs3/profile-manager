"""Zakładki i adresy startowe (bez sieci i bez przeglądarki)."""
import json, os, sys, tempfile
from pathlib import Path
os.environ['APPDATA'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from pm import bookmarks, core

# --- normalize_url: brak schematu nie może wywalać przeglądarki
N = core.normalize_url
assert N("example.com") == "https://example.com"
assert N(" https://a.pl/x ") == "https://a.pl/x"
assert N("localhost:3000") == "http://localhost:3000" and N("192.168.0.1") == "http://192.168.0.1"
assert N("about:blank") == "about:blank" and N("chrome://version") == "chrome://version" and N("") == ""
assert N("localhostfoo.pl") == "https://localhostfoo.pl"

# --- parsowanie
items = bookmarks.parse_text("# komentarz\nSteam | steamcommunity.com\n\nhttps://www.wikipedia.org\nSteam dup | https://steamcommunity.com\n")
assert items == [{"title": "Steam", "url": "https://steamcommunity.com"},
                 {"title": "wikipedia.org", "url": "https://www.wikipedia.org"}], items
assert bookmarks.parse_text(bookmarks.to_text(items)) == items

# --- nowy profil: plik Bookmarks z poprawnymi korzeniami i sumą kontrolną
data = Path(tempfile.mkdtemp()) / "data"
assert bookmarks.seed(data, items) == 2
doc = json.loads((data / "Default" / "Bookmarks").read_text("utf-8"))
bar = doc["roots"]["bookmark_bar"]["children"]
assert [b["name"] for b in bar] == ["Steam", "wikipedia.org"] and [b["id"] for b in bar] == ["4", "5"]
assert doc["checksum"] == bookmarks._checksum(doc["roots"]) and set(doc["roots"]) == {"bookmark_bar", "other", "synced"}
assert json.loads((data / "Default" / "Preferences").read_text())["bookmark_bar"]["show_on_all_tabs"] is True

# --- drugie uruchomienie: nic nowego; zakładka usunięta przez użytkownika nie wraca
assert bookmarks.seed(data, items) == 0
doc["roots"]["bookmark_bar"]["children"].pop(0)
(data / "Default" / "Bookmarks").write_text(json.dumps(doc), "utf-8")
assert bookmarks.seed(data, items) == 0
assert len(json.loads((data / "Default" / "Bookmarks").read_text())["roots"]["bookmark_bar"]["children"]) == 1

# --- nowa zakładka w configu dopisuje się obok istniejących (też tych użytkownika), duplikat adresu jest pomijany
doc = json.loads((data / "Default" / "Bookmarks").read_text())
doc["roots"]["other"]["children"].append({"id": "40", "name": "Moja", "type": "url", "url": "https://example.com/", "guid": "g"})
(data / "Default" / "Bookmarks").write_text(json.dumps(doc), "utf-8")
more = items + bookmarks.parse_text("Ex | https://EXAMPLE.com\nNowa | https://new.pl/a")
assert bookmarks.seed(data, more) == 1          # Ex pominięty (już jest w „Other”), Nowa dodana
doc = json.loads((data / "Default" / "Bookmarks").read_text())
names = [b["name"] for b in doc["roots"]["bookmark_bar"]["children"]]
assert names == ["wikipedia.org", "Nowa"] and doc["roots"]["bookmark_bar"]["children"][-1]["id"] == "41"
assert doc["checksum"] == bookmarks._checksum(doc["roots"])
print("Zakładki OK")
