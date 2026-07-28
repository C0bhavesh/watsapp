import base64
import hashlib
import hmac


def verify_shopify_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """Shopify webhook HMAC: base64(HMAC-SHA256(raw body, client secret)) — NOT hex like Meta."""
    if not header_value:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_value.strip())
