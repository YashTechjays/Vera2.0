import {
  Activity,
  Bot,
  PhoneCall,
  BarChart3,
  Database,
  KeyRound,
  ListTree,
  Mic,
  Settings,
  Users,
  type LucideIcon,
} from "lucide-react"

export type NavItem = {
  title: string
  to: string
  icon: LucideIcon
  /** When set, the item only renders if the user holds this permission. */
  permission?: string
  /** When true, the item only renders for platform super admins. */
  superAdminOnly?: boolean
}

export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity, permission: "calls:read" },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Voice Lab", to: "/voice-lab", icon: Mic, permission: "voice_lab:sandbox" },
  { title: "Call History", to: "/call-history", icon: PhoneCall, permission: "calls:read" },
  { title: "Analytics", to: "/analytics", icon: BarChart3, permission: "calls:read" },
  { title: "Tenant Access", to: "/tenant-access", icon: KeyRound, superAdminOnly: true },
  { title: "Agent Prompt", to: "/agent-prompt", icon: Bot, superAdminOnly: true },
  { title: "IVR Playbooks", to: "/ivr-playbooks", icon: ListTree, superAdminOnly: true },
  { title: "Users", to: "/users", icon: Users, permission: "users:read" },
  { title: "Settings", to: "/settings", icon: Settings },
]

export type NavContext = {
  permissions: string[]
  isSuperAdmin: boolean
  isElevated: boolean
}

/** The nav items visible to the current user, in display order.
 *  - permission-gated items need the permission;
 *  - platform-only items (superAdminOnly) show for super admins only;
 *  - tenant-scoped items are hidden from a super admin until they elevate
 *    (un-elevated they'd 403 anyway); tenant users always see them;
 *  - for a super admin, platform-only items are surfaced first. */
export function visibleNavFor({ permissions, isSuperAdmin, isElevated }: NavContext): NavItem[] {
  const visible = navItems.filter((item) => {
    if (item.permission && !permissions.includes(item.permission)) return false
    if (item.superAdminOnly) return isSuperAdmin
    if (isSuperAdmin) return isElevated
    return true
  })
  return isSuperAdmin
    ? [...visible.filter((i) => i.superAdminOnly), ...visible.filter((i) => !i.superAdminOnly)]
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
