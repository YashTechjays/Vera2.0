"""Integration tests for the tenant-admin surfaces: user invites, role admin,
provider toggles, and API-key management — over a live RLS-enforcing connection.

The `admin` persona holds TENANT_ADMIN (which now includes `users:manage`,
`roles:manage`, `tenant:auth:configure`, `apikeys:manage`); `norole` holds nothing.
"""

from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.api_key import resolve_api_key
from control_plane.auth.session import InMemorySessionStore
from control_plane.email import InMemoryEmailSender
from tests.integration.control_plane.conftest import RBACWorld, _mint
from vera_core.models import AppUser


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


# --- auth/me (session hydration, no permission gate) -------------------------


async def test_me_self_read_allowed_without_any_permission(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # A user with no roles can still read their OWN session — /me has no permission gate.
    resp = await client.get("/api/v1/auth/me", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["roles"] == []
    assert data["permissions"] == []
    assert data["email"] == "norole@test.example"


async def test_me_lists_admin_roles_and_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/auth/me", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "TENANT_ADMIN" in data["roles"]
    assert len(data["permissions"]) > 0


# --- users / invitations -----------------------------------------------------


async def test_invite_returns_link_and_captures_email(
    client: httpx.AsyncClient, rbac_world: RBACWorld, email_sender: InMemoryEmailSender
) -> None:
    before = len(email_sender.sent)
    resp = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "newhire@test.example", "name": "New Hire", "send_email": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["invite_url"].startswith("http")
    assert "token=" in data["invite_url"]
    assert data["email_sent"] is True
    # Email captured by the in-memory sender, link present, raw token never logged.
    assert len(email_sender.sent) == before + 1
    assert "token=" in email_sender.sent[-1].body
    assert email_sender.sent[-1].to == "newhire@test.example"


async def test_invite_records_inviter_and_role_grant_provenance(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")

    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={
            "email": "provenance@test.example",
            "send_email": False,
            "role_ids": [supervisor_id],
        },
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]

    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'admin@test.example'")
        )
        invited_by = await session.scalar(
            text("SELECT invited_by FROM app_user WHERE id = :i").bindparams(i=UUID(user_id))
        )
        granted_by, granted_at = (
            await session.execute(
                text(
                    "SELECT granted_by, granted_at FROM user_role"
                    " WHERE app_user_id = :i AND role_id = :r"
                ).bindparams(i=UUID(user_id), r=UUID(supervisor_id))
            )
        ).one()

    assert invited_by == admin_id
    assert granted_by == admin_id
    assert granted_at is not None


async def test_invite_rejects_invalid_email(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """Sprint-2 #11 — invalid email address must be rejected with 422."""
    resp = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "not-an-email", "send_email": False},
    )
    assert resp.status_code == 422, resp.text


async def test_invite_link_only_skips_email(
    client: httpx.AsyncClient, rbac_world: RBACWorld, email_sender: InMemoryEmailSender
) -> None:
    before = len(email_sender.sent)
    resp = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "linkonly@test.example", "name": "", "send_email": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["email_sent"] is False
    assert len(email_sender.sent) == before  # no email delivered


async def test_invite_then_accept_activates_user(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "accepts@test.example", "name": "Accepts", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    accept = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text
    # No password provider configured in this tenant ⇒ no MFA enforced ⇒ active now.
    assert accept.json()["data"]["mfa_required"] is False

    async with admin_sessionmaker() as session:
        status = await session.scalar(
            text("SELECT status FROM app_user WHERE id = :i").bindparams(i=UUID(user_id))
        )
        identity = await session.scalar(
            text(
                "SELECT count(*) FROM user_identity WHERE app_user_id = :i"
                " AND provider_type = 'password'"
            ).bindparams(i=UUID(user_id))
        )
    assert status == "active"
    assert identity == 1


async def test_accept_is_single_use(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "once@test.example", "send_email": False},
    )
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]
    first = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert second.status_code == 401  # token consumed


async def test_deactivate_user(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "deactivate@test.example", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    resp = await client.post(
        f"/api/v1/users/{user_id}/deactivate",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    async with admin_sessionmaker() as session:
        status = await session.scalar(
            text("SELECT status FROM app_user WHERE id = :i").bindparams(i=UUID(user_id))
        )
    assert status == "deactivated"


# --- invitations/validate -----------------------------------------------------


async def test_validate_valid_token(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A fresh invite token returns state='valid'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_valid@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "valid"
    # No PHI in response body
    assert "email" not in data
    assert "user_id" not in data
    assert "name" not in data
    # Cache-Control header set
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_deactivated_user_token(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A token whose user has been deactivated returns state='deactivated'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_deactivated@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    # Deactivate the user before they accept
    deactivate = await client.post(
        f"/api/v1/users/{user_id}/deactivate",
        headers=_auth(rbac_world.admin_token),
    )
    assert deactivate.status_code == 200, deactivate.text

    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "deactivated"
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_bogus_token_returns_invalid(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A missing/bogus token returns state='invalid' (not a 4xx error)."""
    tid = rbac_world.tenant_id
    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": "this-is-not-a-real-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "invalid"
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_used_token_returns_invalid(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """After a token is consumed by accept, validate returns state='invalid'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_used@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    # Consume the token via accept
    accept = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "strong-password-123"},
    )
    assert accept.status_code == 200, accept.text

    # Now validate returns invalid (token consumed, user is no longer "invited")
    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "invalid"


async def test_admin_endpoint_denied_without_permission(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(
        "/api/v1/users",
        headers=_auth(rbac_world.norole_token),
    )
    assert resp.status_code == 403


# --- roles -------------------------------------------------------------------


async def test_create_custom_role_appears_in_list(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "BILLING_VIEWER", "permission_ids": []},
    )
    assert created.status_code == 200, created.text
    listing = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    names = {r["name"] for r in listing.json()["data"]}
    assert "BILLING_VIEWER" in names  # custom role
    assert "SUPER_ADMIN" not in names  # platform-tier role, excluded from GET /roles


async def test_virtual_assistant_role_seeded_and_visible(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    listing = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    names = {r["name"] for r in listing.json()["data"]}
    assert "VIRTUAL_ASSISTANT" in names


async def test_assign_and_revoke_role(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "roleme@test.example", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")

    assign = await client.post(
        f"/api/v1/users/{user_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": supervisor_id},
    )
    assert assign.status_code == 200, assign.text
    # Duplicate assignment is a conflict.
    dup = await client.post(
        f"/api/v1/users/{user_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": supervisor_id},
    )
    assert dup.status_code == 409

    revoke = await client.request(
        "DELETE",
        f"/api/v1/users/{user_id}/roles/{supervisor_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert revoke.status_code == 200, revoke.text
    revoke_again = await client.request(
        "DELETE",
        f"/api/v1/users/{user_id}/roles/{supervisor_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert revoke_again.status_code == 404


async def test_super_admin_role_not_tenant_assignable(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "noescalate@test.example", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    # SUPER_ADMIN carries platform perms, so a tenant must not be able to assign it
    # (privilege escalation guard) — resolved via direct SQL since GET /roles no
    # longer surfaces platform-tier roles to a tenant session (see the test above).
    async with admin_sessionmaker() as session:
        super_admin_id = await session.scalar(
            text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
        )
    resp = await client.post(
        f"/api/v1/users/{user_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": str(super_admin_id)},
    )
    assert resp.status_code == 403


async def test_invite_cannot_grant_platform_role(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The invite seam must apply the same guard as assign_role (it bypasses it).
    async with admin_sessionmaker() as session:
        super_admin_id = await session.scalar(
            text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
        )
    resp = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={
            "email": "viainvite@test.example",
            "send_email": False,
            "role_ids": [str(super_admin_id)],
        },
    )
    assert resp.status_code == 403


async def test_create_role_rejects_platform_permission(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A tenant must not be able to mint a custom role carrying a platform perm.
    async with admin_sessionmaker() as session:
        platform_perm_id = await session.scalar(
            text("SELECT id FROM permission WHERE code = 'platform:elevations:read'")
        )
    resp = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "SNEAKY", "permission_ids": [str(platform_perm_id)]},
    )
    assert resp.status_code == 403


async def test_list_permissions_hides_platform_tier(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    codes = {p["code"] for p in rows}
    assert "roles:manage" in codes  # tenant-tier codes are present
    assert not any(c.startswith("platform:") for c in codes)  # platform tier hidden
    assert all(set(p) == {"id", "code", "description"} for p in rows)


async def test_list_permissions_requires_roles_manage(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/permissions", headers=_auth(rbac_world.norole_token))
    assert resp.status_code == 403


async def test_get_role_detail_includes_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    perms = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    users_read = next(p for p in perms.json()["data"] if p["code"] == "users:read")
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "DETAIL_ROLE", "permission_ids": [users_read["id"]]},
    )
    role_id = created.json()["data"]["id"]

    detail = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["name"] == "DETAIL_ROLE"
    assert data["is_system"] is False
    assert [p["code"] for p in data["permissions"]] == ["users:read"]


async def test_get_role_detail_unknown_id_is_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get(f"/api/v1/roles/{uuid4()}", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 404


async def test_create_role_accepts_description(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "DESCRIBED", "description": "Sees billing", "permission_ids": []},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["description"] == "Sees billing"


async def test_patch_role_updates_fields_and_permissions(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    perms = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    users_read = next(p["id"] for p in perms.json()["data"] if p["code"] == "users:read")
    calls_read = next(p["id"] for p in perms.json()["data"] if p["code"] == "calls:read")
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "PATCH_ME", "permission_ids": [users_read]},
    )
    role_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "PATCHED", "description": "now different", "permission_ids": [calls_read]},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["name"] == "PATCHED"
    assert data["description"] == "now different"
    assert [p["code"] for p in data["permissions"]] == ["calls:read"]

    # Omitted fields stay unchanged (None = leave alone).
    partial = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"description": "only this"},
    )
    assert partial.json()["data"]["name"] == "PATCHED"
    assert partial.json()["data"]["description"] == "only this"


async def test_patch_role_overlapping_permission_set_is_not_a_conflict(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # Regression: keeping a permission across the edit (the RoleDialog's normal
    # flow — it always sends the full selected set) must not collide with itself
    # under the (role_id, permission_id) unique constraint.
    perms = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    users_read = next(p["id"] for p in perms.json()["data"] if p["code"] == "users:read")
    calls_read = next(p["id"] for p in perms.json()["data"] if p["code"] == "calls:read")
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "OVERLAP_PATCH", "permission_ids": [users_read]},
    )
    role_id = created.json()["data"]["id"]

    patched = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"permission_ids": [users_read, calls_read]},
    )
    assert patched.status_code == 200, patched.text
    codes = {p["code"] for p in patched.json()["data"]["permissions"]}
    assert codes == {"users:read", "calls:read"}


async def test_patch_system_role_is_forbidden(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    resp = await client.patch(
        f"/api/v1/roles/{supervisor_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "HIJACKED"},
    )
    assert resp.status_code == 403  # explicit ownership check, not a silent 0-row update


async def test_patch_role_rejects_platform_permission(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "NO_PLATFORM_VIA_PATCH", "permission_ids": []},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        platform_perm_id = await session.scalar(
            text("SELECT id FROM permission WHERE code = 'platform:elevations:read'")
        )
    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"permission_ids": [str(platform_perm_id)]},
    )
    assert resp.status_code == 403


async def test_patch_role_unknown_permission_is_400_and_dup_name_409(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    a = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "PATCH_A", "permission_ids": []},
    )
    await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "PATCH_B", "permission_ids": []},
    )
    a_id = a.json()["data"]["id"]

    bad_perm = await client.patch(
        f"/api/v1/roles/{a_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"permission_ids": [str(uuid4())]},
    )
    assert bad_perm.status_code == 400

    dup = await client.patch(
        f"/api/v1/roles/{a_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "PATCH_B"},
    )
    assert dup.status_code == 409  # unique (tenant_id, name)


async def test_patch_role_invalidates_holder_permission_cache(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # End-to-end proof of the cache rule: norole gains users:read via a custom
    # role, then loses it the moment PATCH strips the permission — no TTL wait.
    perms = await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))
    users_read = next(p["id"] for p in perms.json()["data"] if p["code"] == "users:read")
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "TEMP_USER_READERS", "permission_ids": [users_read]},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        norole_id = await session.scalar(
            text(
                "SELECT id FROM app_user WHERE email = 'norole@test.example' AND tenant_id = :t"
            ).bindparams(t=rbac_world.tenant_id)
        )

    denied = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert denied.status_code == 403  # baseline: no permission (and it is now cached)

    assign = await client.post(
        f"/api/v1/users/{norole_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": role_id},
    )
    assert assign.status_code == 200, assign.text
    allowed = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert allowed.status_code == 200  # assign invalidated norole's cache

    stripped = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"permission_ids": []},
    )
    assert stripped.status_code == 200, stripped.text
    denied_again = await client.get("/api/v1/users", headers=_auth(rbac_world.norole_token))
    assert denied_again.status_code == 403  # PATCH invalidated every holder's cache

    # Cleanup so later tests see norole with no roles.
    await client.request(
        "DELETE",
        f"/api/v1/users/{norole_id}/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
    )


async def test_delete_role_blocked_while_held_then_succeeds(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "DELETE_ME", "permission_ids": []},
    )
    role_id = created.json()["data"]["id"]
    async with admin_sessionmaker() as session:
        norole_id = await session.scalar(
            text(
                "SELECT id FROM app_user WHERE email = 'norole@test.example' AND tenant_id = :t"
            ).bindparams(t=rbac_world.tenant_id)
        )
    await client.post(
        f"/api/v1/users/{norole_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": role_id},
    )

    blocked = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers={**_auth(rbac_world.admin_token), **_idem()}
    )
    assert blocked.status_code == 409  # DECISION: no silent cascade
    assert blocked.json()["data"]["holder_count"] == 1

    await client.request(
        "DELETE",
        f"/api/v1/users/{norole_id}/roles/{role_id}",
        headers=_auth(rbac_world.admin_token),
    )
    deleted = await client.request(
        "DELETE", f"/api/v1/roles/{role_id}", headers={**_auth(rbac_world.admin_token), **_idem()}
    )
    assert deleted.status_code == 200, deleted.text
    listing = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    assert "DELETE_ME" not in {r["name"] for r in listing.json()["data"]}


async def test_delete_system_role_forbidden_and_unknown_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    forbidden = await client.request(
        "DELETE",
        f"/api/v1/roles/{supervisor_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
    )
    assert forbidden.status_code == 403
    missing = await client.request(
        "DELETE", f"/api/v1/roles/{uuid4()}", headers={**_auth(rbac_world.admin_token), **_idem()}
    )
    assert missing.status_code == 404


async def test_cannot_revoke_own_last_roles_manage_source(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text(
                "SELECT id FROM app_user WHERE email = 'admin@test.example' AND tenant_id = :t"
            ).bindparams(t=rbac_world.tenant_id)
        )
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    tenant_admin_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "TENANT_ADMIN")
    # TENANT_ADMIN is the admin persona's only role → its only roles:manage source.
    resp = await client.request(
        "DELETE",
        f"/api/v1/users/{admin_id}/roles/{tenant_admin_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 409


async def test_revoking_one_of_two_roles_manage_sources_is_allowed(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text(
                "SELECT id FROM app_user WHERE email = 'admin@test.example' AND tenant_id = :t"
            ).bindparams(t=rbac_world.tenant_id)
        )
    perms = (await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ]
    roles_manage = next(p["id"] for p in perms if p["code"] == "roles:manage")
    extra = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "SECOND_MANAGER", "permission_ids": [roles_manage]},
    )
    extra_id = extra.json()["data"]["id"]
    await client.post(
        f"/api/v1/users/{admin_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": extra_id},
    )
    # Removing the EXTRA role is fine — TENANT_ADMIN still grants roles:manage.
    resp = await client.request(
        "DELETE",
        f"/api/v1/users/{admin_id}/roles/{extra_id}",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text


async def _make_sole_manager(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
    *,
    role_name: str,
    email: str,
) -> tuple[UUID, str]:
    """A fresh tenant user whose ONLY role grants roles:manage — never the shared
    `rbac_world` admin persona, whose TENANT_ADMIN source must stay intact across
    the whole test session. Returns (role_id, the fresh user's session token)."""
    perms = (await client.get("/api/v1/permissions", headers=_auth(rbac_world.admin_token))).json()[
        "data"
    ]
    roles_manage = next(p["id"] for p in perms if p["code"] == "roles:manage")
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": role_name, "permission_ids": [roles_manage]},
    )
    role_id = created.json()["data"]["id"]

    async with admin_sessionmaker() as session, session.begin():
        fresh = AppUser(
            tenant_id=rbac_world.tenant_id,
            gcip_uid=None,
            email=email,
            name="Sole Manager",
            status="active",
        )
        session.add(fresh)
        await session.flush()
        fresh_id = fresh.id

    assign = await client.post(
        f"/api/v1/users/{fresh_id}/roles",
        headers=_auth(rbac_world.admin_token),
        json={"role_id": role_id},
    )
    assert assign.status_code == 200, assign.text

    fresh_token = await _mint(
        session_store, user_id=fresh_id, tenant_id=rbac_world.tenant_id, email=email
    )
    return role_id, fresh_token


async def test_patch_role_dropping_own_last_roles_manage_source_is_blocked(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    role_id, fresh_token = await _make_sole_manager(
        client,
        rbac_world,
        admin_sessionmaker,
        session_store,
        role_name="SOLE_MANAGER_SELF",
        email="sole_manager_self@test.example",
    )

    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(fresh_token), **_idem()},
        json={"permission_ids": []},
    )
    assert resp.status_code == 409

    detail = await client.get(f"/api/v1/roles/{role_id}", headers=_auth(rbac_world.admin_token))
    assert [p["code"] for p in detail.json()["data"]["permissions"]] == ["roles:manage"]


async def test_patch_role_dropping_roles_manage_by_a_different_admin_is_allowed(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    session_store: InMemorySessionStore,
) -> None:
    # The guard is self-only: a DIFFERENT admin (holding roles:manage via their own
    # TENANT_ADMIN) editing someone else's role does not lock themselves out — the
    # original holder losing access is legitimate admin de-provisioning.
    role_id, _fresh_token = await _make_sole_manager(
        client,
        rbac_world,
        admin_sessionmaker,
        session_store,
        role_name="SOLE_MANAGER_OTHER",
        email="sole_manager_other@test.example",
    )

    resp = await client.patch(
        f"/api/v1/roles/{role_id}",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"permission_ids": []},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["permissions"] == []


async def test_list_user_roles(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "haroles@test.example", "send_email": False, "role_ids": [supervisor_id]},
    )
    user_id = invite.json()["data"]["user_id"]

    resp = await client.get(f"/api/v1/users/{user_id}/roles", headers=_auth(rbac_world.admin_token))
    assert resp.status_code == 200, resp.text
    assert [r["name"] for r in resp.json()["data"]] == ["SUPERVISOR"]


async def test_list_user_roles_unknown_user_404_and_norole_403(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    missing = await client.get(
        f"/api/v1/users/{uuid4()}/roles", headers=_auth(rbac_world.admin_token)
    )
    assert missing.status_code == 404
    denied = await client.get(
        f"/api/v1/users/{uuid4()}/roles", headers=_auth(rbac_world.norole_token)
    )
    assert denied.status_code == 403


async def test_list_role_holders(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    created = await client.post(
        "/api/v1/roles",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "HOLDER_TEST_ROLE", "permission_ids": []},
    )
    role_id = created.json()["data"]["id"]

    # No holders yet.
    empty = await client.get(
        f"/api/v1/roles/{role_id}/users", headers=_auth(rbac_world.admin_token)
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == []

    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={
            "email": "holder@test.example",
            "name": "Holder",
            "send_email": False,
            "role_ids": [role_id],
        },
    )
    user_id = invite.json()["data"]["user_id"]

    holders = await client.get(
        f"/api/v1/roles/{role_id}/users", headers=_auth(rbac_world.admin_token)
    )
    assert holders.status_code == 200, holders.text
    data = holders.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == user_id
    assert data[0]["name"] == "Holder"
    # Minimum-necessary: no email/status here — that data is gated by users:read.
    assert "email" not in data[0]
    assert "status" not in data[0]


async def test_list_role_holders_of_system_role(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # System roles (tenant_id IS NULL) are visible via catalog RLS, and their
    # holders come from this tenant's own user_role rows.
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    tenant_admin_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "TENANT_ADMIN")
    holders = await client.get(
        f"/api/v1/roles/{tenant_admin_id}/users", headers=_auth(rbac_world.admin_token)
    )
    assert holders.status_code == 200, holders.text
    names = {h["name"] for h in holders.json()["data"]}
    assert "Admin" in names  # the shared rbac_world admin persona


async def test_list_role_holders_unknown_role_404_and_norole_403(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    missing = await client.get(
        f"/api/v1/roles/{uuid4()}/users", headers=_auth(rbac_world.admin_token)
    )
    assert missing.status_code == 404
    denied = await client.get(
        f"/api/v1/roles/{uuid4()}/users", headers=_auth(rbac_world.norole_token)
    )
    assert denied.status_code == 403


# --- providers ---------------------------------------------------------------


async def test_provider_toggle_and_mfa_rule(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    # Enabling password without MFA enforcement is rejected (spec rule).
    bad = await client.patch(
        "/api/v1/auth/providers/password",
        headers=_auth(rbac_world.admin_token),
        json={"enabled": True, "enforce_mfa": False},
    )
    assert bad.status_code == 400, bad.text

    good = await client.patch(
        "/api/v1/auth/providers/password",
        headers=_auth(rbac_world.admin_token),
        json={"enabled": True, "enforce_mfa": True},
    )
    assert good.status_code == 200, good.text

    listing = await client.get("/api/v1/auth/providers", headers=_auth(rbac_world.admin_token))
    password = next(p for p in listing.json()["data"] if p["provider_type"] == "password")
    assert password["enabled"] is True
    assert password["enforce_mfa"] is True


async def test_provider_unknown_type_rejected(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.patch(
        "/api/v1/auth/providers/telepathy",
        headers=_auth(rbac_world.admin_token),
        json={"enabled": True, "enforce_mfa": True},
    )
    assert resp.status_code == 400


# --- API keys ----------------------------------------------------------------


async def test_api_key_issue_verify_revoke(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tid = rbac_world.tenant_id
    created = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "AppScript intake", "scope": "intake:write"},
    )
    assert created.status_code == 200, created.text
    payload = created.json()["data"]
    token = payload["token"]
    key_id = payload["id"]

    # The verifier resolves the token to the tenant + scope (RLS-enforcing session).
    principal = await resolve_api_key(token, rls_sessionmaker)
    assert principal is not None
    assert principal.tenant_id == tid
    assert principal.scope == "intake:write"

    # A tampered secret does not verify.
    assert await resolve_api_key(token + "x", rls_sessionmaker) is None

    revoke = await client.post(
        f"/api/v1/api-keys/{key_id}/revoke",
        headers=_auth(rbac_world.admin_token),
    )
    assert revoke.status_code == 200, revoke.text
    # Revoked key no longer authenticates.
    assert await resolve_api_key(token, rls_sessionmaker) is None

    listing = await client.get("/api/v1/api-keys", headers=_auth(rbac_world.admin_token))
    row = next(k for k in listing.json()["data"] if k["id"] == key_id)
    assert row["revoked"] is True
    assert "token" not in row  # secret never returned on list


async def test_api_key_unknown_scope_rejected(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "typo", "scope": "intake:wrlte"},
    )
    assert resp.status_code == 400, resp.text


async def test_api_key_scopes_catalog(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    resp = await client.get(
        "/api/v1/api-keys/scopes",
        headers=_auth(rbac_world.admin_token),
    )
    assert resp.status_code == 200, resp.text
    catalog = resp.json()["data"]
    codes = {entry["code"] for entry in catalog}
    assert "intake:write" in codes
    assert all(entry["description"] for entry in catalog)  # every scope is labelled


async def test_api_key_duplicate_active_name_rejected(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    first = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "dupe", "scope": "intake:write"},
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "dupe", "scope": "intake:write"},
    )
    assert second.status_code == 409, second.text


async def test_api_key_name_reusable_after_revoke(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    first = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "rotate", "scope": "intake:write"},
    )
    assert first.status_code == 200, first.text
    revoke = await client.post(
        f"/api/v1/api-keys/{first.json()['data']['id']}/revoke",
        headers=_auth(rbac_world.admin_token),
    )
    assert revoke.status_code == 200, revoke.text
    # Revoking the prior key frees the name for a fresh one.
    again = await client.post(
        "/api/v1/api-keys",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"name": "rotate", "scope": "intake:write"},
    )
    assert again.status_code == 200, again.text
