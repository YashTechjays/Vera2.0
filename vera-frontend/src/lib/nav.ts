import {
  Activity,
  Bot,
  PhoneCall,
  BarChart3,
  Database,
  KeyRound,
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
  { title: "Live Monitoring", to: "/", icon: Activity },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Voice Lab", to: "/voice-lab", icon: Mic, permission: "calls:read" },
  { title: "Call History", to: "/call-history", icon: PhoneCall },
  { title: "Analytics", to: "/analytics", icon: BarChart3 },
  { title: "Tenant Access", to: "/tenant-access", icon: KeyRound, superAdminOnly: true },
  { title: "Agent Prompt", to: "/agent-prompt", icon: Bot, superAdminOnly: true },
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
