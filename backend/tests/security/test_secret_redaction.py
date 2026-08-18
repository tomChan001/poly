import json

from backend.app.core.security import REDACTED, redact_sensitive, redact_text


def test_nested_credentials_are_redacted_without_changing_business_fields() -> None:
    payload = {
        "authorization": "Bearer live-token",
        "market_id": "market-1",
        "nested": {
            "api_key": "key-value",
            "privateKey": "wallet-key",
            "signature": "signed-value",
            "cookie": "session=value",
        },
    }

    redacted = redact_sensitive(payload)
    serialized = json.dumps(redacted)

    assert redacted["market_id"] == "market-1"
    assert all(secret not in serialized for secret in ("live-token", "key-value", "wallet-key", "signed-value", "session=value"))
    assert serialized.count(REDACTED) == 5


def test_authorization_headers_are_redacted_from_error_text() -> None:
    message = "request failed Authorization: Bearer abc.def.ghi Cookie: sid=secret"

    redacted = redact_text(message)

    assert "abc.def.ghi" not in redacted
    assert "sid=secret" not in redacted
