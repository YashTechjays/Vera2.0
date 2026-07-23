"""Platform (SUPER_ADMIN) voice-cascade LLM model override.

Global (no tenant_id, no RLS) — a platform surface a SUPER_ADMIN curates, applying to
every tenant's calls. Basic validation only (see vera_core.services.model_config); no
live Vertex AI check (control_plane's prod service account has no aiplatform IAM grant
yet — see adr/devops-todo.md). Delivery to the DB-free agent worker happens separately,
at dispatch time (queue_dispatcher.py / voice_lab.py), not from this router.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.common import AppSettings
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import platform_require
from control_plane.deps import get_idempotency_store, platform_scoped_session
from control_plane.exceptions import CustomAPIException, CustomAPIResponse, DefaultExceptionCode
from control_plane.idempotency import (
    PLATFORM_IDEM_SCOPE,
    claim_or_conflict,
    require_idempotency_key,
)
from control_plane.responses import ResponseModel, ok
from vera_core.models import VoiceModelConfig
from vera_core.services.model_config import (
    InvalidModelName,
    get_active_llm_config,
    list_llm_config_history,
    reset_llm_model,
    save_llm_model,
)

router = APIRouter(prefix="/platform/llm-config", tags=["platform-llm-config"])

PlatformSession = Annotated[AsyncSession, Depends(platform_scoped_session)]
_READ = platform_require("platform:llm_config:read")
_WRITE = platform_require("platform:llm_config:write")


class SaveLlmConfigRequest(BaseModel):
    model: str


class LlmConfigState(BaseModel):
    provider: str | None
    model: str | None
    is_default: bool
    created_at: datetime | None
    created_by_user_id: UUID | None


def _state(row: VoiceModelConfig | None) -> LlmConfigState:
    if row is None:
        return LlmConfigState(
            provider=None, model=None, is_default=True, created_at=None, created_by_user_id=None
        )
    return LlmConfigState(
        provider=row.provider,
        model=row.model,
        is_default=row.model is None,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


@router.get(
    "",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def get_llm_config(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[LlmConfigState]:
    return ok(_state(await get_active_llm_config(session)))


@router.get(
    "/history",
    response_model=ResponseModel[list[LlmConfigState]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED, DefaultExceptionCode.FORBIDDEN
    ),
)
async def get_llm_config_history(
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
) -> ResponseModel[list[LlmConfigState]]:
    rows = await list_llm_config_history(session)
    return ok([_state(row) for row in rows])


@router.put(
    "",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def save_llm_config(
    body: SaveLlmConfigRequest,
    request: Request,
    session: PlatformSession,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[LlmConfigState]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    try:
        row = await save_llm_model(session, body.model, created_by_user_id=caller.user_id)
    except InvalidModelName as exc:
        raise CustomAPIException(DefaultExceptionCode.VALIDATION_ERROR, message=str(exc)) from exc
    return ok(_state(row))


@router.post(
    "/reset",
    response_model=ResponseModel[LlmConfigState],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.BAD_REQUEST,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def reset_llm_config(
    request: Request,
    session: PlatformSession,
    settings: AppSettings,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[LlmConfigState]:
    await claim_or_conflict(
        get_idempotency_store(request),
        PLATFORM_IDEM_SCOPE,
        caller.user_id,
        idempotency_key,
        settings.idempotency_lock_ttl_seconds,
    )
    await reset_llm_model(session, created_by_user_id=caller.user_id)
    return ok(_state(await get_active_llm_config(session)))
