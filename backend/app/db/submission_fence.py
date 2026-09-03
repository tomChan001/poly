"""Durable global fence for new order submissions and policy/control writes."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One stable advisory-lock namespace protects the linearization point shared by
# opening control, risk policy versions, and new order submissions.  Do not
# acquire a per-execution lock before this fence for a new opening.
SUBMISSION_FENCE_LOCK = "poly-submission-fence"


async def lock_submission_fence(session: AsyncSession) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": SUBMISSION_FENCE_LOCK},
    )
