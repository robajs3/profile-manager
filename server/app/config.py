from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://pm:pm@db:5432/pm"
    secret_key: str                # podpis JWT
    encryption_key: str            # klucz Fernet do szyfrowania blobów profili
    server_name: str = "Profile Manager"

    # closed = tylko konta tworzone z CLI | invite = rejestracja z kodem zaproszenia | open = każdy
    registration_mode: str = "closed"
    max_users: int = 0             # 0 = bez limitu
    max_profile_mb: int = 2048     # maks. rozmiar jednego profilu
    user_quota_mb: int = 0         # 0 = bez limitu na użytkownika
    max_profiles_per_user: int = 0 # 0 = bez limitu
    keep_backups: int = 1          # ile poprzednich rewizji trzymać
    lease_ttl_seconds: int = 90    # po tylu sekundach bez heartbeatu blokada wygasa
    token_days: int = 30
    data_dir: str = "/data"


settings = Settings()
