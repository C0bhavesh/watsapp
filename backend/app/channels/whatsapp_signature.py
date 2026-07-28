import hashlib
import hmac

_PREFIX = "sha256="


def verify_meta_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """Meta webhook HMAC: sha256=<hex>(HMAC-SHA256(raw body, app secret)).

    NOT base64 like Shopify.
    """
    if not header_value or not header_value.startswith(_PREFIX):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    candidate = header_value[len(_PREFIX) :].strip()
    try:
        provided = candidate.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided)
