import {
  Activity,
  PhoneCall,
  BarChart3,
  Database,
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
}

export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Call History", to: "/call-history", icon: PhoneCall },
  { title: "Analytics", to: "/analytics", icon: BarChart3 },
  { title: "Users", to: "/users", icon: Users, permission: "users:read" },
  { title: "Settings", to: "/settings", icon: Settings },
]
