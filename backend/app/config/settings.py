from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_master_key: str
    database_url: str = ""
    shop_domain: str = "thetavas.myshopify.com"
    shopify_api_version: str = "2026-07"
    request_timeout_seconds: float = 20.0
    app_env: str = "dev"
    cron_secret: str = ""
