from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.app.domain.enums import BookSafety
from backend.app.domain.market import NormalizedBook


class BookSynchronizationError(ValueError):
    def __init__(self, message: str, *, code: str = "STALE_BOOK") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SynchronizedBooks:
    kalshi: NormalizedBook
    polymarket: NormalizedBook


class BookState:
    """Tracks whether a streamed book can be trusted for execution decisions."""

    def __init__(self) -> None:
        self.book: NormalizedBook | None = None
        self.numeric_sequence: int | None = None
        self.safety = BookSafety.UNSAFE

    def replace(self, book: NormalizedBook, *, numeric_sequence: int | None) -> None:
        self.book = book
        self.numeric_sequence = numeric_sequence
        self.safety = BookSafety.SAFE if numeric_sequence is not None else BookSafety.UNSAFE

    def accept_sequence(self, numeric_sequence: int) -> None:
        if self.numeric_sequence is None or numeric_sequence != self.numeric_sequence + 1:
            self.safety = BookSafety.UNSAFE
            return
        self.numeric_sequence = numeric_sequence



def synchronize_books(
    kalshi: NormalizedBook,
    polymarket: NormalizedBook,
    *,
    now: datetime,
    maximum_age: timedelta,
    maximum_arrival_gap: timedelta,
) -> SynchronizedBooks:
    # `received_at` protects the local pipeline, while `captured_at` protects
    # against a venue returning an old snapshot in a newly completed response.
    timestamps = (
        kalshi.captured_at,
        kalshi.received_at,
        polymarket.captured_at,
        polymarket.received_at,
    )
    if any(timestamp is None for timestamp in timestamps):
        raise BookSynchronizationError(
            "missing venue freshness evidence",
            code="BOOK_FRESHNESS_EVIDENCE_MISSING",
        )
    complete_timestamps = tuple(timestamp for timestamp in timestamps if timestamp is not None)
    if any(now - timestamp > maximum_age for timestamp in complete_timestamps):
        raise BookSynchronizationError("stale order book")

    arrival_gap = abs(kalshi.received_at - polymarket.received_at)
    if arrival_gap > maximum_arrival_gap:
        raise BookSynchronizationError("order book arrival gap exceeds limit")

    return SynchronizedBooks(kalshi, polymarket)
