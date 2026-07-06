import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import {
  createRole,
  groupPermissionsByPrefix,
  updateRole,
  type Permission,
  type RoleDetail,
} from "@/lib/roles"

type RoleDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new role; a RoleDetail = edit that role. */
  role: RoleDetail | null
  permissions: Permission[]
  onSaved: () => void | Promise<void>
}

export function RoleDialog({ open, onOpenChange, role, permissions, onSaved }: RoleDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{role ? `Edit role: ${role.name}` : "Create role"}</DialogTitle>
          <DialogDescription>
            A role bundles permissions; assign it to users in the section below.
          </DialogDescription>
        </DialogHeader>

        {/* Mounted only while open, keyed by role: a fresh mount seeds the form
            from `role` (create: blank; edit: the role) with no effect required. */}
        {open && (
          <RoleDialogForm
            key={role?.id ?? "create"}
            role={role}
            permissions={permissions}
            onOpenChange={onOpenChange}
            onSaved={onSaved}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

type RoleDialogFormProps = {
  role: RoleDetail | null
  permissions: Permission[]
  onOpenChange: (open: boolean) => void
  onSaved: () => void | Promise<void>
}

function RoleDialogForm({ role, permissions, onOpenChange, onSaved }: RoleDialogFormProps) {
  const [name, setName] = useState(role?.name ?? "")
  const [description, setDescription] = useState(role?.description ?? "")
  const [selected, setSelected] = useState<Set<string>>(
    new Set(role?.permissions.map((p) => p.id) ?? []),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = (id: string, checked: boolean) => {
    setSelected((cur) => {
      const next = new Set(cur)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const handleSave = async () => {
    if (!name.trim()) return
    setSaving(true)
    setError(null)
    try {
      if (role) {
        await updateRole(role.id, {
          name: name.trim(),
          description,
          permission_ids: [...selected],
        })
      } else {
        await createRole(name.trim(), description, [...selected])
      }
      await onSaved()
      onOpenChange(false)
    } catch (err) {
      // 409 = duplicate name; message comes from the server.
      setError(err instanceof ApiError ? err.message : "Could not save the role.")
    } finally {
      setSaving(false)
    }
  }

  let saveLabel = role ? "Save changes" : "Create role"
  if (saving) saveLabel = "Saving…"

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-1">
        <label className="text-xs text-muted-foreground" htmlFor="role-name">
          Name
        </label>
        <Input
          id="role-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. BILLING_VIEWER"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label className="text-xs text-muted-foreground" htmlFor="role-description">
          Description
        </label>
        <Textarea
          id="role-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this role is for"
          rows={2}
        />
      </div>

      <div className="space-y-3">
        <p className="text-xs font-medium text-muted-foreground">Permissions</p>
        {groupPermissionsByPrefix(permissions).map((group) => (
          <div key={group.prefix} className="space-y-1">
            <p className="text-xs font-semibold uppercase tracking-wide">{group.prefix}</p>
            {group.permissions.map((p) => (
              <label key={p.id} className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={selected.has(p.id)}
                  onCheckedChange={(checked) => toggle(p.id, checked === true)}
                />
                <span className="font-mono text-xs">{p.code}</span>
                <span className="truncate text-muted-foreground">{p.description}</span>
              </label>
            ))}
          </div>
        ))}
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button type="button" disabled={saving || !name.trim()} onClick={handleSave}>
          {saveLabel}
        </Button>
      </div>
    </div>
  )
}
