import hashlib
import os

import requests


class ApiError(Exception):
    def __init__(self, status: int, code: str, data: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.data = status, code, data or {}


class _Reader:
    """Plik z licznikiem postępu; requests wyśle go strumieniowo z Content-Length."""
    def __init__(self, path, cb):
        self.f, self.size, self.done, self.cb = open(path, "rb"), os.path.getsize(path), 0, cb

    def __len__(self):
        return self.size

    def read(self, n=-1):
        b = self.f.read(n)
        self.done += len(b)
        if self.cb:
            self.cb(self.done, self.size)
        return b

    def __iter__(self):
        while b := self.read(1 << 20):
            yield b

    def close(self):
        self.f.close()


class Api:
    def __init__(self, settings: dict):
        self.s = settings
        self.sess = requests.Session()

    @property
    def base(self) -> str:
        return self.s["server_url"].rstrip("/")

    def _h(self, extra=None) -> dict:
        h = {"X-Device-Id": self.s["device_id"]}
        if self.s.get("token"):
            h["Authorization"] = f"Bearer {self.s['token']}"
        return {**h, **(extra or {})}

    @staticmethod
    def _err(r) -> ApiError:
        try:
            d = r.json().get("detail")
        except Exception:
            d = None
        if isinstance(d, dict):
            return ApiError(r.status_code, d.get("code", "error"), d)
        if isinstance(d, list):
            return ApiError(r.status_code, "validation")
        return ApiError(r.status_code, str(d or f"http_{r.status_code}"))

    def _json(self, method, path, timeout=20, **kw):
        if not self.base:
            raise ApiError(0, "no_server")
        try:
            r = self.sess.request(method, self.base + path, headers=self._h(), timeout=timeout, **kw)
        except requests.RequestException as e:
            raise ApiError(0, "network", {"msg": str(e)})
        if r.status_code >= 400:
            raise self._err(r)
        return r.json()

    @property
    def dev(self) -> dict:
        return {"device_id": self.s["device_id"], "device_name": self.s["device_name"]}

    # --- konto ---
    def info(self): return self._json("GET", "/server/info")
    def login(self, u, p, code=None): return self._json("POST", "/auth/login", json={"username": u, "password": p, "totp_code": code})
    def register(self, u, p, invite=None): return self._json("POST", "/auth/register", json={"username": u, "password": p, "invite_code": invite})
    def me(self): return self._json("GET", "/auth/me")
    def totp_setup(self): return self._json("POST", "/auth/totp/setup")
    def totp_enable(self, code): return self._json("POST", "/auth/totp/enable", json={"code": code})
    def totp_disable(self, pw, code): return self._json("POST", "/auth/totp/disable", json={"password": pw, "code": code})

    # --- profile ---
    def list_profiles(self): return self._json("GET", "/profiles")
    def create_profile(self, pid, name, config, fp): return self._json("POST", "/profiles", json={"id": pid, "name": name, "config": config, "fingerprint": fp})
    def update_profile(self, pid, **kw): return self._json("PUT", f"/profiles/{pid}", json=kw)
    def delete_profile(self, pid): return self._json("DELETE", f"/profiles/{pid}")
    def get_fingerprint(self, pid): return self._json("GET", f"/profiles/{pid}/fingerprint")["fingerprint"]
    def lock(self, pid): return self._json("POST", f"/profiles/{pid}/lock", json=self.dev)
    def heartbeat(self, pid): return self._json("POST", f"/profiles/{pid}/heartbeat", json=self.dev, timeout=10)
    def unlock(self, pid): return self._json("POST", f"/profiles/{pid}/unlock", json=self.dev)

    def download(self, pid, dest, cb=None):
        try:
            with self.sess.get(f"{self.base}/profiles/{pid}/data", headers=self._h(), stream=True, timeout=(10, 120)) as r:
                if r.status_code >= 400:
                    raise self._err(r)
                total, done, h = int(r.headers.get("Content-Length", 0)), 0, hashlib.sha256()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk); h.update(chunk); done += len(chunk)
                        if cb: cb(done, total)
                want = r.headers.get("X-SHA256")
                if want and h.hexdigest() != want:
                    raise ApiError(0, "checksum_mismatch")
        except requests.RequestException as e:
            raise ApiError(0, "network", {"msg": str(e)})

    def upload(self, pid, src, base_revision, cb=None) -> dict:
        reader = _Reader(src, cb)
        try:
            r = self.sess.put(f"{self.base}/profiles/{pid}/data", data=reader, timeout=(10, 900),
                              headers=self._h({"X-Base-Revision": str(base_revision),
                                               "Content-Type": "application/octet-stream"}))
        except requests.RequestException as e:
            raise ApiError(0, "network", {"msg": str(e)})
        finally:
            reader.close()
        if r.status_code >= 400:
            raise self._err(r)
        return r.json()
