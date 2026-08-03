import { MfaSetupDialog } from "@/components/auth/MfaSetupDialog"
import { ApiKeysSection } from "@/components/settings/ApiKeysSection"
import { ConcurrencySection } from "@/components/settings/ConcurrencySection"
import { IntegrationsSection } from "@/components/settings/IntegrationsSection"
import { RolesSection } from "@/components/settings/RolesSection"
import { SettingsCard } from "@/components/settings/SettingsCard"
import { usePermission } from "@/lib/auth/permissions"
import { useAppSelector } from "@/store/hooks"
import { selectUser } from "@/store/authSlice"

export function Settings() {
  const user = useAppSelector(selectUser)
  const canManageApiKeys = usePermission("apikeys:manage")
  const canManageIntegrations = usePermission("integrations:manage")
  const canManageRoles = usePermission("roles:manage")
  const canManageTenantConfig = usePermission("tenant:config:manage")
  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">Signed in as {user?.email}</p>
      </div>

      <SettingsCard
        title="Security"
        description="Two-factor authentication adds a one-time code at sign-in for extra protection."
      >
        <MfaSetupDialog />
      </SettingsCard>

      {canManageApiKeys && <ApiKeysSection />}

      {canManageRoles && <RolesSection />}

      {canManageIntegrations && <IntegrationsSection />}

      {canManageTenantConfig && <ConcurrencySection />}
    </div>
  )
}
