import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAppSelector } from "@/store/hooks"
import { selectLogoutRedirectPath, selectStatus } from "@/store/authSlice"

export function RequireAuth() {
  const status = useAppSelector(selectStatus)
  const logoutPath = useAppSelector(selectLogoutRedirectPath)
  const location = useLocation()
  if (status === "loading") {
    return <div className="flex min-h-screen items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (status === "anonymous") {
    return <Navigate to={logoutPath} replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
