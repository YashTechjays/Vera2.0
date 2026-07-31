from vera_core.models.enums import AuthEvent


def test_tenant_crud_auth_events_exist() -> None:
    assert AuthEvent.TENANT_CREATED.value == "tenant_created"
    assert AuthEvent.TENANT_UPDATED.value == "tenant_updated"
    assert AuthEvent.TENANT_DEACTIVATED.value == "tenant_deactivated"
    assert AuthEvent.TENANT_REACTIVATED.value == "tenant_reactivated"
    assert AuthEvent.TENANT_USER_INVITED.value == "tenant_user_invited"


def test_new_platform_invite_auth_events_exist() -> None:
    assert AuthEvent.INVITE_RESENT.value == "invite_resent"
    assert AuthEvent.PLATFORM_USER_INVITED.value == "platform_user_invited"
    assert AuthEvent.PLATFORM_INVITE_ACCEPTED.value == "platform_invite_accepted"
    assert AuthEvent.PLATFORM_USER_ACTIVATED.value == "platform_user_activated"
    assert AuthEvent.PLATFORM_USER_DEACTIVATED.value == "platform_user_deactivated"
    assert AuthEvent.PLATFORM_INVITE_RESENT.value == "platform_invite_resent"
