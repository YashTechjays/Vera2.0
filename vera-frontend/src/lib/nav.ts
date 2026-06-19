import {
  Activity,
  PhoneCall,
  BarChart3,
  Database,
  Settings,
  type LucideIcon,
} from "lucide-react"

export type NavItem = {
  title: string
  to: string
  icon: LucideIcon
}

export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity },
  { title: "Data Management", to: "/data-management", icon: Database },
  { title: "Call History", to: "/call-history", icon: PhoneCall },
  { title: "Analytics", to: "/analytics", icon: BarChart3 },
  { title: "Settings", to: "/settings", icon: Settings },
]
