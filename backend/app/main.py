from fastapi import FastAPI

app = FastAPI(title="Thetavas Order Bot")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "thetavas-order-bot"}
