import type { ReactNode } from "react"
import { Navigate } from "react-router-dom"
import { defaultRouteFor, isRouteVisible } from "@/lib/nav"
import { useAppSelector } from "@/store/hooks"
import { selectIsElevated, selectIsSuperAdmin, selectPermissions } from "@/store/authSlice"

/** Wraps a routed page with the same visibility rule as its sidebar entry
 *  (`nav.ts`): if the route isn't in the user's visible nav, redirect to their
 *  first visible item instead of rendering a page they have no access to. */
export function RequireNavRoute({ to, children }: { to: string; children: ReactNode }) {
  const permissions = useAppSelector(selectPermissions)
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const isElevated = useAppSelector(selectIsElevated)
  const ctx = { permissions, isSuperAdmin, isElevated }
  if (isRouteVisible(to, ctx)) return <>{children}</>
  return <Navigate to={defaultRouteFor(ctx)} replace />
}
