from backend.app.services.notifications import InMemoryOutbox, NotificationService


def test_notification_outbox_is_idempotent() -> None:
    outbox = InMemoryOutbox()
    service = NotificationService(outbox)

    first = service.enqueue("mapping-1:exact", "mapping.reviewed", {"status": "exact"})
    second = service.enqueue("mapping-1:exact", "mapping.reviewed", {"status": "exact"})

    assert first is second
    assert len(outbox.events) == 1

