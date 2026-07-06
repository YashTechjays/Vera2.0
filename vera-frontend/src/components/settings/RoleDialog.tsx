import { useState } from "react"

import { Badge } from "@/components/ui/badge"
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
import { Label } from "@/components/ui/label"
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
      <DialogContent showCloseButton className="flex max-h-[85vh] flex-col gap-0 p-0 sm:max-w-2xl">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle className="text-base font-semibold">
            {role ? `Edit role: ${role.name}` : "Create role"}
          </DialogTitle>
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

  const setGroup = (ids: string[], checked: boolean) => {
    setSelected((cur) => {
      const next = new Set(cur)
      for (const id of ids) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }

  const toggle = (id: string, checked: boolean) => setGroup([id], checked)

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
    <>
      <div className="flex min-h-0 flex-1 flex-col gap-4 p-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="role-name">Name</Label>
            <Input
              id="role-name"
              autoFocus={!role}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. BILLING_VIEWER"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="role-description">Description</Label>
            <Textarea
              id="role-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this role is for"
              rows={1}
              className="min-h-9 resize-none"
            />
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <div className="flex items-center justify-between">
            <Label>Permissions</Label>
            <Badge variant="secondary">
              {selected.size} of {permissions.length} selected
            </Badge>
          </div>
          <div className="-mr-2 min-h-0 overflow-y-auto pr-2">
            <div className="grid items-start gap-3 sm:grid-cols-2">
              {groupPermissionsByPrefix(permissions).map((group) => {
                const groupIds = group.permissions.map((p) => p.id)
                const checkedCount = groupIds.filter((id) => selected.has(id)).length
                let groupChecked: boolean | "indeterminate" = false
                if (checkedCount === groupIds.length) groupChecked = true
                else if (checkedCount > 0) groupChecked = "indeterminate"
                return (
                  <div key={group.prefix} className="overflow-hidden rounded-lg border">
                    <label className="flex cursor-pointer items-center justify-between gap-2 border-b bg-muted/50 px-3 py-2">
                      <span className="text-xs font-semibold uppercase tracking-wide">
                        {group.prefix}
                      </span>
                      <Checkbox
                        aria-label={`Select all ${group.prefix} permissions`}
                        checked={groupChecked}
                        onCheckedChange={(checked) => setGroup(groupIds, checked === true)}
                      />
                    </label>
                    <div className="divide-y">
                      {group.permissions.map((p) => (
                        <label
                          key={p.id}
                          className="flex cursor-pointer items-start gap-2.5 px-3 py-2 transition-colors hover:bg-muted/40"
                        >
                          <Checkbox
                            className="mt-0.5"
                            checked={selected.has(p.id)}
                            onCheckedChange={(checked) => toggle(p.id, checked === true)}
                          />
                          <span className="flex min-w-0 flex-col gap-0.5">
                            <span className="font-mono text-xs leading-4">{p.code}</span>
                            {p.description && (
                              <span className="text-xs leading-4 text-muted-foreground">
                                {p.description}
                              </span>
                            )}
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-4 border-t border-border p-4">
        <p className="min-w-0 text-sm text-destructive" role="alert">
          {error}
        </p>
        <div className="flex shrink-0 gap-2">
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            className="min-w-[120px]"
            disabled={saving || !name.trim()}
            onClick={handleSave}
          >
            {saveLabel}
          </Button>
        </div>
      </div>
    </>
  )
}
