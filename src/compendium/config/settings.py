from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_DEFAULT = "insecure-default-change-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COMPENDIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///compendium.db"
    default_loan_period_days: int = 14
    guest_search_enabled: bool = True
    jwt_secret_key: str = INSECURE_JWT_DEFAULT
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8
    hold_expiry_days: int = 30
    hold_pickup_days: int = 3
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    tmdb_api_key: str | None = None
    secure_cookies: bool = False
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
