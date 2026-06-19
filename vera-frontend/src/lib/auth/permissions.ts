import { useAppSelector } from "@/store/hooks"
import { selectPermissions } from "@/store/authSlice"

export function usePermission(code: string): boolean {
  const permissions = useAppSelector(selectPermissions)
  return permissions.includes(code)
}
