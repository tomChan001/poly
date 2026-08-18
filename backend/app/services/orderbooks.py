from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.app.domain.enums import BookSafety
from backend.app.domain.market import NormalizedBook


class BookSynchronizationError(ValueError):
    pass


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

    def replace(self, book: NormalizedBook, *, numeric_sequence: int) -> None:
        self.book = book
        self.numeric_sequence = numeric_sequence
        self.safety = BookSafety.SAFE

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
    if now - kalshi.received_at > maximum_age or now - polymarket.received_at > maximum_age:
        raise BookSynchronizationError("stale order book")

    arrival_gap = abs(kalshi.received_at - polymarket.received_at)
    if arrival_gap > maximum_arrival_gap:
        raise BookSynchronizationError("order book arrival gap exceeds limit")

    return SynchronizedBooks(kalshi, polymarket)
