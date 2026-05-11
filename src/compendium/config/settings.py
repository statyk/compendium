import os
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULT = "insecure-default-change-in-production"
MIN_JWT_SECRET_LENGTH = 32

# Mapping from pydantic field name → the *_FILE env var that can supply its value.
# When the direct env var is absent/empty AND the _FILE var points to a readable
# file, the file's contents (newline-stripped) are used as the field value.
# This mirrors the pattern used by official Docker images (postgres, redis, etc.).
_SECRET_FILE_ENV_MAP: dict[str, str] = {
    "jwt_secret_key": "COMPENDIUM_JWT_SECRET_KEY_FILE",
    "secret_key": "COMPENDIUM_SECRET_KEY_FILE",
    "smtp_password": "COMPENDIUM_SMTP_PASSWORD_FILE",
    "tmdb_api_key": "COMPENDIUM_TMDB_API_KEY_FILE",
    "google_books_api_key": "COMPENDIUM_GOOGLE_BOOKS_API_KEY_FILE",
}


class InsecureConfigError(RuntimeError):
    """Raised when Compendium is asked to start with a known-insecure config."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COMPENDIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _treat_empty_env_strings_as_unset(cls, data: object) -> object:
        # Docker-compose's `${VAR:-}` pattern sets the var to "" when the
        # host-side value is absent. Drop empty strings so int/bool/Literal
        # fields fall back to their Python defaults instead of crashing.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data

    @model_validator(mode="before")
    @classmethod
    def _load_secrets_from_files(cls, data: object) -> object:
        """Honor *_FILE env-var variants for sensitive settings.

        If e.g. COMPENDIUM_JWT_SECRET_KEY_FILE=/run/secrets/jwt_key is set and
        COMPENDIUM_JWT_SECRET_KEY (or its field) is absent, the file's contents
        are used. The direct env var always takes precedence over the file.
        """
        if not isinstance(data, dict):
            return data
        for field, file_env in _SECRET_FILE_ENV_MAP.items():
            if field in data:
                continue  # direct value wins
            file_path = os.environ.get(file_env)
            if not file_path:
                continue
            try:
                with open(file_path) as fh:
                    data[field] = fh.read().rstrip("\r\n")
            except OSError:
                pass  # missing or unreadable — leave unset, let normal validation handle it
        return data

    database_url: str = "sqlite:///compendium.db"
    guest_search_enabled: bool = True
    jwt_secret_key: str = INSECURE_JWT_DEFAULT
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8
    hold_expiry_days: int = 30
    hold_pickup_days: int = 3
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    tmdb_api_key: str | None = None
    google_books_api_key: str | None = None
    book_metadata_source_preference: str = "googlebooks"
    secure_cookies: bool = True
    audit_retention_days: int | None = None
    default_theme: Literal["light", "dark", "auto"] = "light"
    fine_block_threshold_cents: int | None = None
    fine_block_holds: bool = False
    currency_symbol: str = "$"
    currency_symbol_position: Literal["before", "after"] = "before"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_from_address: str | None = None
    smtp_from_name: str = "Compendium"
    notifications_batch_size: int = 50
    notifications_max_attempts: int = 5
    notification_retention_days: int | None = None
    due_soon_days_before: int = 3
    overdue_tiers: str = "3,14,30"
    kiosk_idle_timeout_seconds: int = 60
    # Hard cap on the size of any single bulk-import upload. Defends the
    # daemon from OOM via a multi-GB POST body. Env-only on purpose — DB
    # editability would let a compromised admin token raise the cap to
    # bypass the protection.
    max_upload_bytes: int = 100 * 1024 * 1024
    # Comma-separated list of allowed Host header values for TrustedHostMiddleware.
    # When unset (the default), any Host is accepted — backwards-compatible.
    # Production: set to your public hostname(s),
    # e.g. COMPENDIUM_ALLOWED_HOSTS=library.example.org,www.library.example.org
    allowed_hosts: str | None = None
    # Comma-separated list of trusted reverse-proxy IP addresses. When set,
    # X-Forwarded-For is honored for the per-IP login rate limit; otherwise
    # request.client.host is used directly (prevents header spoofing).
    # e.g. COMPENDIUM_TRUSTED_PROXIES=172.17.0.2,10.0.0.1
    trusted_proxies: str | None = None
    # Library name — printed on patron cards, shown in emails, and (future)
    # in the nav brand. First of several Settings that should migrate to a
    # DB-backed site_setting table when that slice lands; see CLAUDE.md.
    library_name: str = "Compendium"
