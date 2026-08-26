from backend.app.services.notifications import InMemoryOutbox, NotificationService


def test_notification_outbox_is_idempotent() -> None:
    outbox = InMemoryOutbox()
    service = NotificationService(outbox)

    first = service.enqueue("mapping-1:exact", "mapping.reviewed", {"status": "exact"})
    second = service.enqueue("mapping-1:exact", "mapping.reviewed", {"status": "exact"})

    assert first is second
    assert len(outbox.events) == 1


def test_notification_outbox_redacts_credentials_recursively() -> None:
    outbox = InMemoryOutbox()
    service = NotificationService(outbox)

    event = service.enqueue(
        "integration:error",
        "integration.failed",
        {
            "private_key": "secret-private-key",
            "details": {"api_secret": "secret-api", "code": "AUTH_FAILED"},
        },
    )

    assert event.payload == {
        "private_key": "[REDACTED]",
        "details": {"api_secret": "[REDACTED]", "code": "AUTH_FAILED"},
    }


def test_notification_outbox_redacts_nested_sequences() -> None:
    outbox = InMemoryOutbox()
    service = NotificationService(outbox)

    event = service.enqueue(
        "integration:sequence",
        "integration.failed",
        {
            "attempts": [
                {"token": "secret-token"},
                {
                    "children": (
                        {"authorization": "Bearer secret"},
                        {"status": "retry"},
                    )
                },
            ]
        },
    )

    assert event.payload == {
        "attempts": [
            {"token": "[REDACTED]"},
            {
                "children": [
                    {"authorization": "[REDACTED]"},
                    {"status": "retry"},
                ]
            },
        ]
    }
