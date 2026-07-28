import hashlib
import hmac as hmac_lib

from app.channels.whatsapp_signature import verify_meta_hmac

SECRET = "app-secret"
BODY = b'{"entry": []}'


def good_header() -> str:
    digest = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes() -> None:
    assert verify_meta_hmac(BODY, good_header(), SECRET)


def test_tampered_body_fails() -> None:
    assert not verify_meta_hmac(b'{"entry": [1]}', good_header(), SECRET)


def test_missing_header_fails() -> None:
    assert not verify_meta_hmac(BODY, None, SECRET)
    assert not verify_meta_hmac(BODY, "", SECRET)


def test_missing_prefix_fails() -> None:
    digest = hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert not verify_meta_hmac(BODY, digest, SECRET)  # bare hex, no "sha256=" prefix


def test_base64_encoding_is_rejected() -> None:
    import base64

    b64 = base64.b64encode(hmac_lib.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()
    assert not verify_meta_hmac(BODY, f"sha256={b64}", SECRET)  # Shopify's scheme, not Meta's


def test_non_ascii_header_fails_closed() -> None:
    assert not verify_meta_hmac(BODY, "sha256=\xe9\xe9", SECRET)
