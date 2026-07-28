from fastapi import FastAPI
from pydantic import ValidationError

from app.channels.shopify_webhook import router as shopify_webhook_router
from app.config.settings import Settings


def _docs_enabled() -> bool:
    """OpenAPI/docs are served everywhere except prod.

    Read ``app_env`` directly from Settings (not the deps container, to avoid
    building it at import time). A missing ``APP_MASTER_KEY`` means we are not in
    a configured prod deploy, so default to the dev posture (docs on).
    """
    try:
        return Settings().app_env != "prod"  # type: ignore[call-arg]
    except ValidationError:
        return True


_DOCS_ENABLED = _docs_enabled()

app = FastAPI(
    title="Thetavas Order Bot",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

app.include_router(shopify_webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "thetavas-order-bot"}
