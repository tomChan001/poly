from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.market import NormalizedBook
from backend.app.services.orderbooks import (
    BookSafety,
    BookState,
    BookSynchronizationError,
    synchronize_books,
)


def empty_book(market_id: str, received_at: datetime) -> NormalizedBook:
    return NormalizedBook(
        market_id=market_id,
        outcome="yes",
        sequence="1",
        captured_at=received_at,
        received_at=received_at,
        asks=(),
    )


def test_books_received_too_far_apart_are_rejected() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)

    with pytest.raises(BookSynchronizationError, match="arrival gap"):
        synchronize_books(
            empty_book("K", now),
            empty_book("P", now + timedelta(milliseconds=501)),
            now=now + timedelta(seconds=1),
            maximum_age=timedelta(seconds=2),
            maximum_arrival_gap=timedelta(milliseconds=500),
        )


def test_stale_book_is_rejected() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)

    with pytest.raises(BookSynchronizationError, match="stale"):
        synchronize_books(
            empty_book("K", now - timedelta(seconds=3)),
            empty_book("P", now - timedelta(seconds=3)),
            now=now,
            maximum_age=timedelta(seconds=2),
            maximum_arrival_gap=timedelta(milliseconds=500),
        )


def test_stale_exchange_capture_time_is_rejected_even_when_just_received() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    stale = empty_book("P", now)
    stale = NormalizedBook(
        market_id=stale.market_id,
        outcome=stale.outcome,
        sequence=stale.sequence,
        captured_at=now - timedelta(seconds=3),
        received_at=now,
        asks=stale.asks,
    )

    with pytest.raises(BookSynchronizationError, match="stale"):
        synchronize_books(
            empty_book("K", now),
            stale,
            now=now,
            maximum_age=timedelta(seconds=2),
            maximum_arrival_gap=timedelta(milliseconds=500),
        )


def test_sequence_gap_marks_book_unsafe_until_snapshot_replacement() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    state = BookState()
    state.replace(empty_book("K", now), numeric_sequence=10)

    state.accept_sequence(12)

    assert state.safety is BookSafety.UNSAFE

    state.replace(empty_book("K", now), numeric_sequence=20)
    assert state.safety is BookSafety.SAFE


def test_missing_venue_capture_time_fails_closed() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    missing_capture_time = NormalizedBook(
        market_id="K",
        outcome="yes",
        sequence="sha256:content-only",
        captured_at=None,
        received_at=now,
        asks=(),
    )

    with pytest.raises(BookSynchronizationError, match="freshness evidence"):
        synchronize_books(
            missing_capture_time,
            empty_book("P", now),
            now=now,
            maximum_age=timedelta(seconds=2),
            maximum_arrival_gap=timedelta(milliseconds=500),
        )


def test_hash_only_snapshot_cannot_establish_sequence_safety() -> None:
    now = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    state = BookState()

    state.replace(empty_book("K", now), numeric_sequence=None)

    assert state.safety is BookSafety.UNSAFE
