from enum import StrEnum


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class MappingStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    EXACT = "exact"
    CONDITIONAL = "conditional"
    REJECTED = "rejected"
    STALE = "stale"


class ExecutionState(StrEnum):
    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    PRECHECKED = "prechecked"
    SUBMITTED = "submitted"
    PAIRED = "paired"
    PARTIALLY_HEDGED = "partially_hedged"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"


class BookSafety(StrEnum):
    SAFE = "safe"
    UNSAFE = "unsafe"

