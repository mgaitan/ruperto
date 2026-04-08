"""API endpoints for kanban board functionality."""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ruperto.channels.base import ChannelDeliveryError
from ruperto.channels.service import deliver_municipal_case_notifications
from ruperto.dashboard_utils import load_dashboard_identity
from ruperto.models import MunicipalCaseStatus
from ruperto.repository import BusinessRepository, MunicipalCaseNotFoundError
from ruperto.schemas import MunicipalCaseSnapshot, MunicipalCaseStatusUpdateRequest

router = APIRouter(prefix="/api/kanban", tags=["kanban"])
dashboard_identity_dependency = Depends(load_dashboard_identity)


def get_db_session(request: Request) -> AsyncSession:
    """Get database session from request runtime."""
    runtime = request.app.state.runtime
    return runtime.database.session_factory()


@router.get("/municipal/cases")
async def list_municipal_cases(
    request: Request,
    status: MunicipalCaseStatus | None = None,
    area_id: int | None = None,
    identity: dict | None = dashboard_identity_dependency,
) -> list[MunicipalCaseSnapshot]:
    """List municipal cases for kanban board with optional filtering."""
    if identity is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    store_id = identity["store_id"]
    async with get_db_session(request) as session:
        repository = BusinessRepository(session)

        return await repository.list_municipal_cases(
            store_id=store_id,
            status=status,
            area_id=area_id,
        )


@router.patch("/municipal/cases/{case_id}/status")
async def update_case_status(
    request: Request,
    case_id: int,
    payload: MunicipalCaseStatusUpdateRequest,
    identity: dict | None = dashboard_identity_dependency,
) -> MunicipalCaseSnapshot:
    """Update the status of a municipal case."""
    if identity is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with get_db_session(request) as session:
        repository = BusinessRepository(session)
        try:
            updated_case = await repository.update_municipal_case_status(
                case_id=case_id,
                payload=payload,
            )
        except MunicipalCaseNotFoundError as error:
            raise HTTPException(status_code=404, detail="Municipal case not found.") from error
        await session.commit()
    runtime = request.app.state.runtime
    with suppress(ChannelDeliveryError):
        await deliver_municipal_case_notifications(
            session_factory=runtime.database.session_factory,
            settings=runtime.settings,
            case_id=case_id,
        )
    return updated_case
