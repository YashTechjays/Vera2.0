import { useCallback, useEffect, useRef, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/lib/api/client"
import {
  deleteRole,
  getRole,
  listPermissions,
  listRoleHolders,
  listRoles,
  revokeRole,
  type Permission,
  type Role,
  type RoleDetail,
  type RoleHolder,
} from "@/lib/roles"
import { PermissionsTable } from "./PermissionsTable"
import { RoleDialog } from "./RoleDialog"
import { SettingsCard } from "./SettingsCard"
import { UserRolesCard } from "./UserRolesCard"

/** Roles & Permissions settings section. Mount only behind a `roles:manage`
 *  check — every endpoint underneath is gated server-side too. */
export function RolesSection() {
  const [roles, setRoles] = useState<Role[] | null>(null)
  const [permissions, setPermissions] = useState<Permission[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<RoleDetail | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<Role | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [holders, setHolders] = useState<RoleHolder[] | null>(null)
  const [removingHolderId, setRemovingHolderId] = useState<string | null>(null)
  // Which role the open delete dialog belongs to — guards stale holder fetches
  // (same pattern as UserRolesCard.selectedUserIdRef).
  const confirmDeleteIdRef = useRef<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setRoles(await listRoles())
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load roles.")
      setRoles([])
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listRoles()
      .then((rows) => {
        if (!cancelled) {
          setRoles(rows)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load roles.")
          setRoles([])
        }
      })
    listPermissions()
      .then((perms) => {
        if (!cancelled) {
          setPermissions(perms)
          setCatalogError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setCatalogError(err instanceof ApiError ? err.message : "Could not load the permission catalog.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const openCreate = useCallback(() => {
    if (permissions === null) return
    setEditing(null)
    setDialogOpen(true)
  }, [permissions])

  const openEdit = useCallback(
    async (role: Role) => {
      if (permissions === null) return
      try {
        setEditing(await getRole(role.id))
        setDialogOpen(true)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not load the role.")
      }
    },
    [permissions],
  )

  const openDeleteDialog = useCallback((role: Role) => {
    setDeleteError(null)
    setHolders(null)
    setConfirmDelete(role)
    confirmDeleteIdRef.current = role.id
    // Pre-flight: who still holds this role? The backend refuses deletion while
    // anyone does, so surface the holders up front instead of a dead-end 409.
    listRoleHolders(role.id)
      .then((rows) => {
        if (confirmDeleteIdRef.current === role.id) setHolders(rows)
      })
      .catch((err) => {
        if (confirmDeleteIdRef.current !== role.id) return
        // Leave holders null: Delete stays disabled until a check SUCCEEDS —
        // "couldn't verify" must never read as "verified empty".
        setDeleteError(
          err instanceof ApiError ? err.message : "Could not check who holds this role.",
        )
      })
  }, [])

  const closeDeleteDialog = useCallback(() => {
    if (deleting) return
    confirmDeleteIdRef.current = null
    setConfirmDelete(null)
    setDeleteError(null)
    setHolders(null)
  }, [deleting])

  const removeHolder = useCallback(
    async (holder: RoleHolder) => {
      if (!confirmDelete) return
      setRemovingHolderId(holder.id)
      setDeleteError(null)
      try {
        await revokeRole(holder.id, confirmDelete.id)
        setHolders(await listRoleHolders(confirmDelete.id))
      } catch (err) {
        // 409 here = the self-lockout guard (removing your own last roles:manage source).
        setDeleteError(err instanceof ApiError ? err.message : "Could not remove the role.")
      } finally {
        setRemovingHolderId(null)
      }
    },
    [confirmDelete],
  )

  const confirmDeleteRole = useCallback(async () => {
    if (!confirmDelete) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteRole(confirmDelete.id)
      setConfirmDelete(null)
      setHolders(null)
      await refresh()
    } catch (err) {
      // 409 = someone was assigned the role after the pre-flight (server re-checks).
      setDeleteError(err instanceof ApiError ? err.message : "Could not delete the role.")
      // Re-sync the holder list so the dialog shows why.
      try {
        setHolders(await listRoleHolders(confirmDelete.id))
      } catch {
        // keep the existing list; the error banner already explains the failure
      }
    } finally {
      setDeleting(false)
    }
  }, [confirmDelete, refresh])

  return (
    <>
      <SettingsCard
        title="Roles & Permissions"
        description="Roles bundle permissions. System roles are managed by the platform and read-only."
        action={
          <Button type="button" onClick={openCreate} disabled={permissions === null}>
            Create role
          </Button>
        }
      >
        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}

        {catalogError && (
          <p className="text-sm text-destructive" role="alert">
            {catalogError}
          </p>
        )}

        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-[220px]">Name</TableHead>
                <TableHead className="w-24">Type</TableHead>
                <TableHead>Description</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {roles === null && (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    Loading…
                  </TableCell>
                </TableRow>
              )}
              {roles?.map((role) => (
                <TableRow key={role.id}>
                  <TableCell className="font-medium">{role.name}</TableCell>
                  <TableCell>
                    <Badge variant={role.is_system ? "secondary" : "outline"}>
                      {role.is_system ? "System" : "Custom"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {role.description || "—"}
                  </TableCell>
                  <TableCell className="text-right">
                    {!role.is_system && (
                      <span className="inline-flex gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={permissions === null}
                          onClick={() => void openEdit(role)}
                        >
                          Edit
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => openDeleteDialog(role)}
                        >
                          Delete
                        </Button>
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </SettingsCard>

      {/* Outside the collapsible: the card's content unmounts when collapsed, but
          the Create button lives in the always-visible header and must still work. */}
      <RoleDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        role={editing}
        permissions={permissions ?? []}
        onSaved={refresh}
      />

      {/* Delete confirmation with a pre-flight holder check: the backend refuses
          to delete a held role, so show WHO holds it and let the admin remove
          each assignment explicitly (no silent cascade) before deleting. */}
      <Dialog
        open={confirmDelete !== null}
        onOpenChange={(o) => {
          if (!o) closeDeleteDialog()
        }}
      >
        <DialogContent showCloseButton={!deleting} className="gap-0 p-0 sm:max-w-md">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">Delete role?</DialogTitle>
            <DialogDescription>
              {confirmDelete && (
                <>
                  <span className="font-medium text-foreground">{confirmDelete.name}</span> will be
                  permanently deleted. This cannot be undone.
                </>
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2 px-5 pb-4">
            {holders === null && !deleteError && (
              <p className="text-sm text-muted-foreground">Checking who holds this role…</p>
            )}
            {holders?.length === 0 && !deleteError && (
              <p className="text-sm text-muted-foreground">No users hold this role.</p>
            )}
            {holders && holders.length > 0 && (
              <>
                <p className="text-sm">
                  Held by <span className="font-medium">{holders.length}</span>{" "}
                  {holders.length === 1 ? "user" : "users"} — remove it from each of them first:
                </p>
                <ul className="divide-y rounded-lg border">
                  {holders.map((h) => (
                    <li key={h.id} className="flex items-center justify-between gap-3 px-3 py-2">
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-medium">
                          {h.name || "—"}
                        </span>
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={removingHolderId === h.id || deleting}
                        onClick={() => void removeHolder(h)}
                      >
                        {removingHolderId === h.id ? "Removing…" : "Remove"}
                      </Button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {deleteError && (
              <p className="text-sm text-destructive" role="alert">
                {deleteError}
              </p>
            )}
          </div>

          <div className="flex justify-end gap-3 border-t border-border p-4">
            <Button variant="outline" onClick={closeDeleteDialog} disabled={deleting}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => void confirmDeleteRole()}
              disabled={deleting || holders === null || holders.length > 0}
              className="min-w-[120px]"
            >
              {deleting ? "Deleting…" : "Delete role"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <SettingsCard
        title="Permission catalog"
        description="Platform-defined capabilities. Grant them to users by adding them to a role."
      >
        {catalogError && (
          <p className="text-sm text-destructive" role="alert">
            {catalogError}
          </p>
        )}
        <PermissionsTable permissions={permissions ?? []} />
      </SettingsCard>

      <SettingsCard
        title="User role assignment"
        description="Pick a user to see and change the roles they hold."
      >
        <UserRolesCard roles={roles ?? []} />
      </SettingsCard>
    </>
  )
}
