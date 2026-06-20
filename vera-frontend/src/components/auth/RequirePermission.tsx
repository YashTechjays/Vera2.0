import type { ReactNode } from "react"
import { usePermission } from "@/lib/auth/permissions"

export function RequirePermission({
  permission,
  children,
  fallback = null,
}: {
  permission: string
  children: ReactNode
  fallback?: ReactNode
}) {
  return usePermission(permission) ? <>{children}</> : <>{fallback}</>
}
