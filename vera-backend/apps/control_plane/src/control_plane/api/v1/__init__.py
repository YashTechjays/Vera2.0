from fastapi import APIRouter

from control_plane.api.v1.api_keys import router as api_keys_router
from control_plane.api.v1.auth import router as auth_router
from control_plane.api.v1.calls import router as calls_router
from control_plane.api.v1.coaching import router as coaching_router
from control_plane.api.v1.form_schemas import router as form_schemas_router
from control_plane.api.v1.insurance_providers import router as insurance_providers_router
from control_plane.api.v1.integrations import router as integrations_router
from control_plane.api.v1.ivr_playbooks import router as ivr_playbooks_router
from control_plane.api.v1.notifications import router as notifications_router
from control_plane.api.v1.patient_forms import router as patient_forms_router
from control_plane.api.v1.platform import router as platform_router
from control_plane.api.v1.platform_auth import router as platform_auth_router
from control_plane.api.v1.prompts import router as prompts_router
from control_plane.api.v1.providers import router as providers_router
from control_plane.api.v1.roles import router as roles_router
from control_plane.api.v1.tenant_config import router as tenant_config_router
from control_plane.api.v1.users import router as users_router
from control_plane.api.v1.voice_lab import router as voice_lab_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(calls_router)
router.include_router(coaching_router)
router.include_router(notifications_router)
router.include_router(patient_forms_router)
router.include_router(platform_router)
router.include_router(prompts_router)
router.include_router(insurance_providers_router)
router.include_router(form_schemas_router)
router.include_router(ivr_playbooks_router)
router.include_router(platform_auth_router)
router.include_router(users_router)
router.include_router(roles_router)
router.include_router(providers_router)
router.include_router(api_keys_router)
router.include_router(tenant_config_router)
router.include_router(integrations_router)
router.include_router(voice_lab_router)
