// Platform-operator (super admin) endpoints. An elevation is a time-boxed grant
// that lets a platform operator act inside one tenant; tenant-scoped routes only
// work while an active grant exists. All require a platform session + the
// platform:elevations:* permissions (held by SUPER_ADMIN).

import { apiRequest } from "@/lib/api/client"

/** Max break-glass window the backend accepts (8 hours). */
export const MAX_ELEVATION_MINUTES = 480
/** Max reason length the backend accepts. */
export const MAX_ELEVATION_REASON = 2000

export type Elevation = {
  id: string
  target_tenant_id: string
  reason: string
  granted_at: string
  expires_at: string
  ended_at: string | null
}

export type CreateElevationInput = {
  target_tenant_id: string
  reason: string
  duration_minutes: number
}

/** All active (un-ended, un-expired) grants. */
export function listElevations() {
  return apiRequest<Elevation[]>("/platform/elevations")
}

export function createElevation(input: CreateElevationInput) {
  return apiRequest<Elevation>("/platform/elevations", { method: "POST", body: input })
}

export function endElevation(id: string) {
  return apiRequest<null>(`/platform/elevations/${encodeURIComponent(id)}/end`, {
    method: "POST",
  })
}
