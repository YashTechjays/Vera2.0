import type { ReactNode } from "react"
import { Navigate } from "react-router-dom"
import { navItems, useNavContext, visibleNavFor } from "@/lib/nav"

/** Wraps a routed page with the same visibility rule as its sidebar entry
 *  (`nav.ts`): if the route isn't in the user's visible nav, redirect to their
 *  first visible item instead of rendering a page they have no access to. A
 *  route with no matching nav.ts entry at all has nothing to gate, so it's
 *  always visible (matches isRouteVisible's fallback). Computes `visibleNavFor`
 *  once and derives both the check and the redirect target from it, rather
 *  than calling it twice via isRouteVisible/defaultRouteFor. */
export function RequireNavRoute({ to, children }: { to: string; children: ReactNode }) {
  const ctx = useNavContext()
  const visible = visibleNavFor(ctx)
  const hasEntry = navItems.some((item) => item.to === to)
  const isVisible = !hasEntry || visible.some((item) => item.to === to)
  if (isVisible) return <>{children}</>
  return <Navigate to={visible[0]?.to ?? "/settings"} replace />
}
