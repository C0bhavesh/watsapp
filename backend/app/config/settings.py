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
    admin_password: str = ""  # env ADMIN_PASSWORD — Rule 1 third exception (approved 2026-07-30)
    # Vertex AI (Gemini) service-account auth — env only, never stored in the config DB.
    # The service-account JSON is a secret; it is env-sourced and never returned to any UI.
    vertex_credentials_json: str = ""  # env VERTEX_CREDENTIALS_JSON
    vertex_project: str = ""  # env VERTEX_PROJECT
    vertex_location: str = "us-central1"  # env VERTEX_LOCATION
