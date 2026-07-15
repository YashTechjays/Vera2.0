import { PanelLeft, Search, Bell, LogOut } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { useAppDispatch, useAppStore } from "@/store/hooks"
import { logoutThunk, selectLogoutRedirectPath } from "@/store/authSlice"

type TopbarProps = {
  onToggleSidebar: () => void
}

export function Topbar({ onToggleSidebar }: TopbarProps) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const store = useAppStore()

  async function onLogout() {
    await dispatch(logoutThunk())
    // Read the path AFTER the logout reducers run: `logoutPlane` is only captured
    // then, so a render-time selector value predates it (and the persisted plane
    // hint is cleared once login completes) — a platform operator would be
    // bounced to the tenant /login.
    navigate(selectLogoutRedirectPath(store.getState()), { replace: true })
  }

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-background px-4">
      <Button
        variant="ghost"
        size="icon"
        onClick={onToggleSidebar}
        aria-label="Toggle sidebar"
      >
        <PanelLeft className="size-5" />
      </Button>

      <div className="relative hidden w-full max-w-sm sm:block">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <input
          type="search"
          placeholder="Search calls, customers, policies…"
          className="h-9 w-full rounded-md border bg-background pl-9 pr-3 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
        />
      </div>

      <div className="ml-auto flex items-center gap-1">
        <Button variant="ghost" size="icon" aria-label="Notifications">
          <Bell className="size-5" />
        </Button>
        <Button variant="ghost" size="icon" aria-label="Sign out" onClick={onLogout}>
          <LogOut className="size-5" />
        </Button>
      </div>
    </header>
  )
}
