import base64
import hashlib
import hmac as hmac_lib

from app.channels.shopify_signature import verify_shopify_hmac

SECRET = "test-secret"
BODY = b'{"id": 1}'


def good_header() -> str:
    return base64.b64encode(hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()


def test_valid_signature_passes() -> None:
    assert verify_shopify_hmac(BODY, good_header(), SECRET)


def test_tampered_body_fails() -> None:
    assert not verify_shopify_hmac(b'{"id": 2}', good_header(), SECRET)


def test_missing_header_fails() -> None:
    assert not verify_shopify_hmac(BODY, None, SECRET)
    assert not verify_shopify_hmac(BODY, "", SECRET)


def test_hex_encoding_is_rejected() -> None:
    hex_header = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert not verify_shopify_hmac(BODY, hex_header, SECRET)


def test_non_ascii_header_returns_false_without_raising() -> None:
    # Starlette decodes headers latin-1, so a non-ASCII byte reaches us as a str.
    # hmac.compare_digest raises TypeError on non-ASCII str — must be caught, not 500.
    assert verify_shopify_hmac(BODY, "\xe9", SECRET) is False
