from dataclasses import dataclass

from app.config.service import ConfigService

WHATSAPP_SECRET_FIELDS = ("access_token", "app_secret", "verify_token")
WHATSAPP_PLAIN_FIELDS = ("phone_number_id", "waba_id", "api_version")


@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str
    app_secret: str
    verify_token: str
    phone_number_id: str
    waba_id: str
    api_version: str


async def load_whatsapp_config(config: ConfigService) -> WhatsAppConfig | None:
    secrets = {f: await config.get_secret(f"whatsapp:{f}") for f in WHATSAPP_SECRET_FIELDS}
    plains = {f: await config.get_plain(f"whatsapp:{f}") for f in WHATSAPP_PLAIN_FIELDS}
    values = {**secrets, **plains}
    if any(not v for v in values.values()):
        return None
    return WhatsAppConfig(
        access_token=str(values["access_token"]),
        app_secret=str(values["app_secret"]),
        verify_token=str(values["verify_token"]),
        phone_number_id=str(values["phone_number_id"]),
        waba_id=str(values["waba_id"]),
        api_version=str(values["api_version"]),
    )
