from pydantic_settings import BaseSettings, SettingsConfigDict


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
    jwt_secret_key: str = "insecure-default-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 8
    hold_expiry_days: int = 30
    hold_pickup_days: int = 3
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
