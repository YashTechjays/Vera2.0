import { NavLink } from "react-router-dom"
import { Sparkles } from "lucide-react"
import { navItems } from "@/lib/nav"
import { cn } from "@/lib/utils"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin, selectPermissions } from "@/store/authSlice"

type SidebarProps = {
  collapsed: boolean
}

export function Sidebar({ collapsed }: SidebarProps) {
  const permissions = useAppSelector(selectPermissions)
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  // RBAC: hide nav items whose required permission the user lacks, and platform-only
  // items (e.g. Agent Prompt) for anyone who isn't a super admin.
  const items = navItems.filter(
    (item) =>
      (!item.permission || permissions.includes(item.permission)) &&
      (!item.superAdminOnly || isSuperAdmin)
  )
  return (
    <aside
      className={cn(
        "flex h-screen shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground transition-[width] duration-200",
        collapsed ? "w-16" : "w-64"
      )}
    >
      {/* Brand */}
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <Sparkles className="size-6 shrink-0 text-sidebar-primary" />
        {!collapsed && (
          <span className="truncate text-2xl font-bold tracking-tight">
            Vera AI
          </span>
        )}
      </div>

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-1 p-2">
        {items.map(({ title, to, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            title={collapsed ? title : undefined}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                "hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                isActive
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70",
                collapsed && "justify-center px-0"
              )
            }
          >
            <Icon className="size-5 shrink-0" />
            {!collapsed && <span className="truncate">{title}</span>}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t p-3">
        <div
          className={cn(
            "flex items-center gap-3",
            collapsed && "justify-center"
          )}
        >
          <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-sidebar-primary text-xs font-semibold text-sidebar-primary-foreground">
            AV
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">Agent View</p>
              <p className="truncate text-xs text-sidebar-foreground/60">
                Internal tools
              </p>
            </div>
          )}
        </div>
      </div>
    </aside>
  )
}
