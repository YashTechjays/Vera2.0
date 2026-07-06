import { NavLink } from "react-router-dom"
import { Sparkles } from "lucide-react"
import { visibleNavFor } from "@/lib/nav"
import { cn } from "@/lib/utils"
import { useAppSelector } from "@/store/hooks"
import { selectIsElevated, selectIsSuperAdmin, selectPermissions, selectUser } from "@/store/authSlice"

type SidebarProps = {
  collapsed: boolean
}

/** First letters of the first two words, uppercased; the first two characters if
 *  there's only one word (e.g. an email used as the display name fallback). */
// eslint-disable-next-line react-refresh/only-export-components
export function initialsFor(source: string): string {
  const parts = source.trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return source.slice(0, 2).toUpperCase()
}

export function Sidebar({ collapsed }: SidebarProps) {
  const permissions = useAppSelector(selectPermissions)
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const isElevated = useAppSelector(selectIsElevated)
  const user = useAppSelector(selectUser)
  const items = visibleNavFor({ permissions, isSuperAdmin, isElevated })

  // AppUser.name defaults to "" (e.g. some platform/password-only accounts), so
  // fall back to email as the display name; when that fallback fires, show the
  // account tier on the second line instead of repeating the email on both lines.
  const displayName = user?.name?.trim() || user?.email || "Signed in"
  const secondaryLine = !user
    ? ""
    : user.name.trim()
      ? user.email
      : user.account_type === "platform"
        ? "Platform operator"
        : "Tenant member"

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
            {initialsFor(displayName)}
          </div>
          {!collapsed && (
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{displayName}</p>
              <p className="truncate text-xs text-sidebar-foreground/60">
                {secondaryLine}
              </p>
            </div>
          )}
        </div>
      </div>
    </aside>
  )
}
