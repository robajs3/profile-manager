import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from . import auth, profiles
from .config import settings
from .models import Base, engine
from .storage import BLOBS, TMP, check_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_key()
    for d in (BLOBS, TMP):
        d.mkdir(parents=True, exist_ok=True)
    for attempt in range(30):  # poczekaj aż Postgres wstanie
        try:
            with engine.connect() as c:
                c.execute(text("select 1"))
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Profile Manager Server", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(auth.router)
app.include_router(profiles.router)


@app.get("/server/info")
def info():
    return {"name": settings.server_name, "registration_mode": settings.registration_mode,
            "api": 1, "lease_ttl": settings.lease_ttl_seconds}


@app.get("/health")
def health():
    return {"ok": True}
