"""Parser CRX (bez sieci): CRX2 i CRX3 muszą zwracać zip oraz klucz publiczny (stałe ID rozszerzenia)."""
import hashlib, io, os, sys, tempfile, zipfile
from pathlib import Path
os.environ['APPDATA'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
from pm.addons import crx_parse


def varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n: return out


def field(num, data): return varint(num << 3 | 2) + varint(len(data)) + data


pub, other = b"PUBKEY-" + bytes(range(90)), b"OTHERKEY" + bytes(range(60))
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z: z.writestr("manifest.json", '{"name":"x","version":"1"}')
Z = zbuf.getvalue()

# CRX3: dwa klucze, właściwy wskazuje crx_id (pierwsze 16 B sha256 klucza)
header = (field(2, field(1, other) + field(2, b"sig")) + field(2, field(1, pub) + field(2, b"sig"))
          + field(10000, field(1, hashlib.sha256(pub).digest()[:16])))
crx3 = b"Cr24" + (3).to_bytes(4, "little") + len(header).to_bytes(4, "little") + header + Z
zb, key = crx_parse(crx3); assert zb == Z and key == pub, "CRX3"
# CRX2
crx2 = b"Cr24" + (2).to_bytes(4, "little") + len(pub).to_bytes(4, "little") + (3).to_bytes(4, "little") + pub + b"sig" + Z
zb, key = crx_parse(crx2); assert zb == Z and key == pub, "CRX2"
assert crx_parse(Z) == (Z, None)
from pm import addons
tmp = Path(tempfile.mkdtemp())
(tmp / "ext.crx").write_bytes(crx3)
r = addons.install_from_file(tmp / "ext.crx", "chrome")
assert r["slug"] == addons.ext_id_from_key(pub) and len(r["slug"]) == 32, r
import json
mf = json.loads((addons.path(r["slug"], "chrome") / "manifest.json").read_text())
assert mf["key"], "brak key w manifeście (ID nie byłoby stałe)"
folder = tmp / "Extensions" / ("a" * 32) / "1.0_0"; folder.mkdir(parents=True)
(folder / "manifest.json").write_text('{"name":"Folderowy","version":"2"}'); (folder / "_metadata").mkdir(); (folder / "_metadata" / "verified_contents.json").write_text("{}")
r2 = addons.install_from_file(folder, "chrome"); assert r2["slug"] == "a" * 32 and r2["name"] == "Folderowy", r2
assert not (addons.path("a" * 32, "chrome") / "_metadata").exists(), "_metadata nie usunięty"
assert {a["slug"] for a in addons.installed("chrome")} == {r["slug"], "a" * 32}
print("CRX OK (parser + instalacja z pliku i z folderu)")
