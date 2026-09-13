from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import (
    Principal,
    Role,
    require_authenticated,
    require_role,
)
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.pair_previews import PairPreview, expire_preview

router = APIRouter(
    prefix="/api/pairs",
    tags=["pairs"],
    dependencies=[Depends(require_authenticated)],
)


class PairReviewRequest(BaseModel):
    status: MappingStatus
    checklist: dict[str, bool]
    truth_table: list[dict[str, Decimal]]
    notes: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class PairReviewView(ExecutablePair):
    preview: PairPreview | None = None


@router.get("")
async def list_pairs(
    container: Annotated[ApplicationContainer, Depends(get_container)],
    with_preview: bool = False,
) -> list[PairReviewView]:
    pairs = await container.executable_pairs.list()
    previews: list[PairPreview | None] = [None] * len(pairs)
    if with_preview and pairs:
        policy = await container.risk_policies.refresh()
        service = await container.build_pair_previews()
        previews = list(await service.evaluate_many(pairs, policy))
        current_policy = await container.risk_policies.refresh()
        response_time = datetime.now(UTC)
        previews = [
            expire_preview(preview, response_time) if preview else None
            for preview in previews
        ]
        if (
            current_policy is None
            or policy is None
            or current_policy.version != policy.version
        ):
            previews = [
                replace(
                    preview,
                    eligible=False,
                    rejection_reasons=tuple(
                        dict.fromkeys(
                            (*preview.rejection_reasons, "RISK_POLICY_CHANGED")
                        )
                    ),
                )
                if preview
                else None
                for preview in previews
            ]
    return [
        PairReviewView(
            **{
                field.name: getattr(pair, field.name)
                for field in fields(ExecutablePair)
            },
            preview=preview,
        )
        for pair, preview in zip(pairs, previews, strict=True)
    ]


@router.post("/{pair_id}/review")
async def review_pair(
    pair_id: str,
    payload: PairReviewRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    principal: Annotated[Principal, Depends(require_role(Role.REVIEWER))],
) -> ExecutablePair:
    try:
        return await container.executable_pairs.review(
            pair_id,
            status=payload.status,
            checklist=payload.checklist,
            truth_table=payload.truth_table,
            notes=payload.notes,
            reviewer=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pair not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
