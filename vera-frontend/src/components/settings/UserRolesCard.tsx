import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  RichSelect,
  RichSelectContent,
  RichSelectItem,
  RichSelectTrigger,
  RichSelectValue,
} from "@/components/ui/rich-select"
import { ApiError } from "@/lib/api/client"
import { listUsers, type UserSummary } from "@/lib/auth/api"
import { assignRole, listUserRoles, revokeRole, type Role } from "@/lib/roles"

/** Pick a user, see their roles, add or remove one. Assign/revoke are audited and
 *  cache-invalidated server-side; the server also rejects platform-privileged roles
 *  (403) and revoking your own last roles:manage source (409) — show its message. */
export function UserRolesCard({ roles }: { roles: Role[] }) {
  const [users, setUsers] = useState<UserSummary[]>([])
  const [selectedUserId, setSelectedUserId] = useState("")
  const [userRoles, setUserRoles] = useState<Role[] | null>(null)
  const [addRoleId, setAddRoleId] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listUsers()
      .then((rows) => {
        if (!cancelled) setUsers(rows)
      })
      .catch((err) => {
        // Needs users:read on top of roles:manage; surface the denial plainly.
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Could not load users.")
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refreshUserRoles = useCallback(async (userId: string) => {
    try {
      setUserRoles(await listUserRoles(userId))
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the user's roles.")
      setUserRoles([])
    }
  }, [])

  // Selecting a user resets the roles view and kicks off its fetch. This lives in the
  // handler (not a useEffect keyed on selectedUserId) because synchronously calling
  // setState in an effect body trips react-hooks/set-state-in-effect; the async fetch
  // callback in refreshUserRoles still sets state from a promise, which is fine.
  const handleSelectUser = useCallback(
    (userId: string) => {
      setSelectedUserId(userId)
      setUserRoles(null)
      setAddRoleId("")
      if (userId) void refreshUserRoles(userId)
    },
    [refreshUserRoles],
  )

  const handleAssign = useCallback(async () => {
    if (!selectedUserId || !addRoleId) return
    setBusy(true)
    try {
      await assignRole(selectedUserId, addRoleId)
      setAddRoleId("")
      await refreshUserRoles(selectedUserId)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not assign the role.")
    } finally {
      setBusy(false)
    }
  }, [selectedUserId, addRoleId, refreshUserRoles])

  const handleRevoke = useCallback(
    async (role: Role) => {
      if (!selectedUserId) return
      setBusy(true)
      try {
        await revokeRole(selectedUserId, role.id)
        await refreshUserRoles(selectedUserId)
      } catch (err) {
        // 409 here = the self-lockout guard; the server message explains it.
        setError(err instanceof ApiError ? err.message : "Could not remove the role.")
      } finally {
        setBusy(false)
      }
    },
    [selectedUserId, refreshUserRoles],
  )

  const assignable = roles.filter((r) => !userRoles?.some((held) => held.id === r.id))

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-medium">User role assignment</h3>
        <p className="text-sm text-muted-foreground">
          Pick a user to see and change the roles they hold.
        </p>
      </div>

      <RichSelect value={selectedUserId} onValueChange={handleSelectUser}>
        <RichSelectTrigger className="w-80">
          <RichSelectValue placeholder={users.length ? "Select a user" : "No users"} />
        </RichSelectTrigger>
        <RichSelectContent>
          {users.map((u) => (
            <RichSelectItem key={u.id} value={u.id} caption={u.email}>
              {u.name || u.email}
            </RichSelectItem>
          ))}
        </RichSelectContent>
      </RichSelect>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {selectedUserId && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {userRoles === null && <p className="text-sm text-muted-foreground">Loading…</p>}
            {userRoles?.length === 0 && (
              <p className="text-sm text-muted-foreground">No roles yet.</p>
            )}
            {userRoles?.map((role) => (
              <Badge key={role.id} variant="outline" className="gap-1">
                {role.name}
                <button
                  type="button"
                  aria-label={`Remove ${role.name}`}
                  className="ml-1 text-muted-foreground hover:text-destructive"
                  disabled={busy}
                  onClick={() => void handleRevoke(role)}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <RichSelect value={addRoleId} onValueChange={setAddRoleId}>
              <RichSelectTrigger className="w-72">
                <RichSelectValue placeholder="Add a role…" />
              </RichSelectTrigger>
              <RichSelectContent>
                {assignable.map((r) => (
                  <RichSelectItem key={r.id} value={r.id} caption={r.description}>
                    {r.name}
                  </RichSelectItem>
                ))}
              </RichSelectContent>
            </RichSelect>
            <Button type="button" disabled={busy || !addRoleId} onClick={() => void handleAssign()}>
              Assign
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
