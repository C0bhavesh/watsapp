"""Send a real WhatsApp text message via send_text.

Run: python -m scripts.smoke_whatsapp --to <E.164>
Requires whatsapp:* config already seeded (access_token/app_secret/verify_token/
phone_number_id/waba_id/api_version) via the admin panel or a seed script.
"""

import argparse
import asyncio

import httpx

from app.channels.whatsapp_config import load_whatsapp_config
from app.channels.whatsapp_sender import send_text
from app.deps import get_container


async def main(to: str) -> None:
    c = get_container()
    cfg = await load_whatsapp_config(c.config)
    if cfg is None:
        raise SystemExit("whatsapp config incomplete -- see module docstring")
    async with httpx.AsyncClient() as http:
        result = await send_text(http, cfg, to, "Thetavas bot smoke test: live send check.")
    # Never print the destination number or response body -- status/wamid only.
    print(f"ok={result.ok} status_code={result.status_code} wamid={result.wamid}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="E.164 recipient, e.g. +917575072795")
    args = parser.parse_args()
    asyncio.run(main(args.to))
