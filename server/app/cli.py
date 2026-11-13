"""Administracja:  docker compose exec app python -m app.cli <komenda>"""
import argparse
import getpass
import secrets
import sys
from datetime import timedelta

from sqlalchemy import select


def main():
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-keys")
    cu = sub.add_parser("create-user"); cu.add_argument("username"); cu.add_argument("--admin", action="store_true")
    ci = sub.add_parser("create-invite"); ci.add_argument("--days", type=int, default=7)
    sub.add_parser("list-users")
    r2 = sub.add_parser("reset-2fa"); r2.add_argument("username")
    du = sub.add_parser("delete-user"); du.add_argument("username")
    a = ap.parse_args()

    if a.cmd == "gen-keys":  # nie wymaga konfiguracji
        from cryptography.fernet import Fernet
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}\nENCRYPTION_KEY={Fernet.generate_key().decode()}")
        return

    from .models import Base, Invite, SessionLocal, User, engine, utcnow
    from .security import hash_password
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if a.cmd == "create-user":
            pw = getpass.getpass("Hasło (min. 8 znaków): ")
            if len(pw) < 8 or pw != getpass.getpass("Powtórz: "):
                sys.exit("Hasła różne lub za krótkie")
            db.add(User(username=a.username.lower(), password_hash=hash_password(pw), is_admin=a.admin))
            db.commit(); print("OK")
        elif a.cmd == "create-invite":
            inv = Invite(code=secrets.token_urlsafe(12), expires_at=utcnow() + timedelta(days=a.days))
            db.add(inv); db.commit(); print(inv.code)
        elif a.cmd == "list-users":
            for u in db.scalars(select(User)):
                print(u.id, u.username, "admin" if u.is_admin else "", "2FA" if u.totp_enabled else "")
        else:
            u = db.scalar(select(User).where(User.username == a.username.lower()))
            if not u: sys.exit("Nie ma takiego użytkownika")
            if a.cmd == "reset-2fa":
                u.totp_enabled, u.totp_secret, u.backup_codes = False, None, []
            else:
                db.delete(u)
            db.commit(); print("OK")


if __name__ == "__main__":
    main()
