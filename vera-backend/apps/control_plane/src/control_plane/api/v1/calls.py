"""Example protected endpoint proving the full chain:
bearer token -> verified identity -> tenant context -> require("calls:read")
-> tenant-scoped (RLS) session -> audited allow/deny.
"""

from fastapi import APIRouter

from control_plane.api.v1.common import TenantId
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import require
from control_plane.exceptions import CustomAPIResponse, DefaultExceptionCode
from control_plane.responses import ResponseModel, ok
from vera_core.schemas import CallSummary

router = APIRouter(tags=["calls"])


@router.get(
    "/calls",
    response_model=ResponseModel[list[CallSummary]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_calls(
    _tenant_id: TenantId,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[list[CallSummary]]:
    # TODO(vera-2.x): real calls table + outbound Twilio SIP dispatch state.
    # Empty list proves the authz chain end-to-end without inventing schema.
    return ok([])
