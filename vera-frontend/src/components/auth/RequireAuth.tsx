import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAppSelector } from "@/store/hooks"
import { selectStatus, selectTenantSlug } from "@/store/authSlice"

const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

export function RequireAuth() {
  const status = useAppSelector(selectStatus)
  const slug = useAppSelector(selectTenantSlug) ?? DEFAULT_SLUG
  const location = useLocation()
  if (status === "loading") {
    return <div className="flex min-h-screen items-center justify-center text-muted-foreground">Loading…</div>
  }
  if (status === "anonymous") {
    return <Navigate to={`/tenants/${slug}/login`} replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
