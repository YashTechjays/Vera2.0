import {
  Activity,
  Bot,
  Building,
  Building2,
  Cpu,
  PhoneCall,
  BarChart3,
  Database,
  FileText,
  KeyRound,
  ListTree,
  Mic,
  Settings,
  SlidersHorizontal,
  Users,
  type LucideIcon,
} from "lucide-react"
import { useAppSelector } from "@/store/hooks"
import { selectIsElevated, selectIsSuperAdmin, selectPermissions } from "@/store/authSlice"

export type NavItem = {
  title: string
  to: string
  icon: LucideIcon
  /** When set, the item only renders if the user holds this permission.
   *  `platform:*` permissions mark platform-plane items (see visibleNavFor). */
  permission?: string
}

export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity, permission: "calls:read" },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Voice Lab", to: "/voice-lab", icon: Mic, permission: "voice_lab:sandbox" },
  { title: "Call History", to: "/call-history", icon: PhoneCall, permission: "calls:read" },
  { title: "Analytics", to: "/analytics", icon: BarChart3, permission: "reports:dashboard" },
  { title: "Tenant Access", to: "/tenant-access", icon: KeyRound, permission: "platform:elevations:read" },
  { title: "Tenants", to: "/platform-tenants", icon: Building, permission: "platform:tenants:manage" },
  { title: "Platform Operators", to: "/platform-operators", icon: Users, permission: "platform:users:read" },
  { title: "Platform Settings", to: "/platform-settings", icon: SlidersHorizontal, permission: "platform:tenants:manage" },
  { title: "Agent Prompt", to: "/agent-prompt", icon: Bot, permission: "platform:prompts:read" },
  { title: "Insurance Providers", to: "/insurance-providers", icon: Building2, permission: "platform:insurance_providers:read" },
  { title: "IVR Playbooks", to: "/ivr-playbooks", icon: ListTree, permission: "platform:ivr_playbooks:read" },
  { title: "Form Schemas", to: "/form-schemas", icon: FileText, permission: "platform:form_schemas:read" },
  { title: "Voice Model", to: "/voice-model", icon: Cpu, permission: "platform:llm_config:read" },
  { title: "Users", to: "/users", icon: Users, permission: "users:read" },
  { title: "Settings", to: "/settings", icon: Settings },
]

/** Platform-plane items are identified by their permission tier, not a role flag —
 *  privilege comes from an RBAC grant, never from who the user "is". */
function isPlatformItem(item: NavItem): boolean {
  return item.permission?.startsWith("platform:") ?? false
}

export type NavContext = {
  permissions: string[]
  isSuperAdmin: boolean
  isElevated: boolean
}

/** The nav items visible to the current user, in display order.
 *  - permission-gated items need the permission;
 *  - platform-tier items (`platform:*` permission) additionally require a
 *    platform account — the grant is the gate, the account type the backstop;
 *  - tenant-scoped items are hidden from a super admin until they elevate
 *    (un-elevated they'd 403 anyway); tenant users always see them;
 *  - for a super admin, platform items are surfaced first. */
export function visibleNavFor({ permissions, isSuperAdmin, isElevated }: NavContext): NavItem[] {
  const visible = navItems.filter((item) => {
    if (item.permission && !permissions.includes(item.permission)) return false
    if (isPlatformItem(item)) return isSuperAdmin
    if (isSuperAdmin) return isElevated
    return true
  })
  return isSuperAdmin
    ? [...visible.filter(isPlatformItem), ...visible.filter((i) => !isPlatformItem(i))]
    : visible
}

/** True if `to` appears in the current user's visible nav — gates a route the same
 *  way its sidebar entry is gated, without duplicating the permission logic.
 *  A route with no matching nav entry has nothing to gate, so it's always visible. */
export function isRouteVisible(to: string, ctx: NavContext): boolean {
  const item = navItems.find((i) => i.to === to)
  if (!item) return true
  return visibleNavFor(ctx).includes(item)
}

/** Where to send a user who can't (or shouldn't) land on the route they hit —
 *  their first visible nav item. Settings carries no permission gate, so this is
 *  never empty for an authenticated tenant user. */
export function defaultRouteFor(ctx: NavContext): string {
  return visibleNavFor(ctx)[0]?.to ?? "/settings"
}

/** The current user's NavContext, assembled from the auth store — the single
 *  place Sidebar and RequireNavRoute read permissions/elevation from, so they
 *  can never diverge on what's visible. */
export function useNavContext(): NavContext {
  const permissions = useAppSelector(selectPermissions)
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const isElevated = useAppSelector(selectIsElevated)
  return { permissions, isSuperAdmin, isElevated }
}
