from fastapi import APIRouter

from control_plane.api.v1.api_keys import router as api_keys_router
from control_plane.api.v1.auth import router as auth_router
from control_plane.api.v1.calls import router as calls_router
from control_plane.api.v1.platform import router as platform_router
from control_plane.api.v1.platform_auth import router as platform_auth_router
from control_plane.api.v1.providers import router as providers_router
from control_plane.api.v1.roles import router as roles_router
from control_plane.api.v1.users import router as users_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(calls_router)
router.include_router(platform_router)
router.include_router(platform_auth_router)
router.include_router(users_router)
router.include_router(roles_router)
router.include_router(providers_router)
router.include_router(api_keys_router)
