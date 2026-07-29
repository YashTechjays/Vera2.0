import { useAppSelector } from "@/store/hooks"
import { selectPermissions } from "@/store/authSlice"

export function usePermission(code: string): boolean {
  // Test membership INSIDE the selector so the subscription compares a boolean. Selecting
  // the array and testing outside re-renders on every /me refresh (a fresh array each
  // time), and on every dispatch while user is null (`?? []` mints a new array per run).
  return useAppSelector((s) => selectPermissions(s).includes(code))
}
