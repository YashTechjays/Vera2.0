import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAppSelector } from "@/store/hooks"
import { selectStatus } from "@/store/authSlice"

export function RequireAuth() {
  const status = useAppSelector(selectStatus)
  const location = useLocation()
  if (status === "loading") {
    return <div className="flex min-h-screen items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
