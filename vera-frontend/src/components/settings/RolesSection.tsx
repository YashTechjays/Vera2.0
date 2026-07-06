import { useCallback, useEffect, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
  listRoles,
  type Permission,
  type Role,
  type RoleDetail,
} from "@/lib/roles"
import { PermissionsTable } from "./PermissionsTable"
import { RoleDialog } from "./RoleDialog"
import { UserRolesCard } from "./UserRolesCard"

/** Roles & Permissions settings section. Mount only behind a `roles:manage`
 *  check — every endpoint underneath is gated server-side too. */
export function RolesSection() {
  const [roles, setRoles] = useState<Role[] | null>(null)
  const [permissions, setPermissions] = useState<Permission[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<RoleDetail | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

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
      .then((p) => {
        if (!cancelled) setPermissions(p)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load the permission catalog.")
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

  const handleDelete = useCallback(
    async (role: Role) => {
      if (!window.confirm(`Delete role "${role.name}"? This cannot be undone.`)) return
      setDeletingId(role.id)
      try {
        await deleteRole(role.id)
        await refresh()
      } catch (err) {
        // 409: "N user(s) still hold this role — remove it from them first".
        setError(err instanceof ApiError ? err.message : "Could not delete the role.")
      } finally {
        setDeletingId(null)
      }
    },
    [refresh],
  )

  return (
    <section className="space-y-4">
      <div className="flex items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium">Roles &amp; Permissions</h2>
          <p className="text-sm text-muted-foreground">
            Roles bundle permissions. System roles are managed by the platform and read-only.
          </p>
        </div>
        <Button type="button" onClick={openCreate} disabled={permissions === null}>
          Create role
        </Button>
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {roles === null && (
              <TableRow>
                <TableCell colSpan={3} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {roles?.map((role) => (
              <TableRow key={role.id}>
                <TableCell className="font-medium">
                  <span className="flex items-center gap-2">
                    {role.name}
                    {role.is_system && <Badge variant="secondary">System</Badge>}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground">{role.description}</TableCell>
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
                        disabled={deletingId === role.id}
                        onClick={() => void handleDelete(role)}
                      >
                        {deletingId === role.id ? "Deleting…" : "Delete"}
                      </Button>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <RoleDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        role={editing}
        permissions={permissions ?? []}
        onSaved={refresh}
      />

      <PermissionsTable permissions={permissions ?? []} />

      <UserRolesCard roles={roles ?? []} />
    </section>
  )
}
