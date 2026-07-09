import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { Permission } from "@/lib/roles"

/** Read-only permission catalog. Permissions are defined in code and seeded by the
 *  platform — "managing" one means adding it to a role, never editing it here. */
export function PermissionsTable({ permissions }: { permissions: Permission[] }) {
  return (
    <div className="rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Code</TableHead>
            <TableHead>Description</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {permissions.length === 0 && (
            <TableRow>
              <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                Loading…
              </TableCell>
            </TableRow>
          )}
          {permissions.map((p) => (
            <TableRow key={p.id}>
              <TableCell className="font-mono text-xs">{p.code}</TableCell>
              <TableCell className="text-muted-foreground">{p.description}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
