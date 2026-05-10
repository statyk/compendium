from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULT = "insecure-default-change-in-production"


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
    # Library name — printed on patron cards, shown in emails, and (future)
    # in the nav brand. First of several Settings that should migrate to a
    # DB-backed site_setting table when that slice lands; see CLAUDE.md.
    library_name: str = "Compendium"
