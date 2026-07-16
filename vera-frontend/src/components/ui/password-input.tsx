import * as React from "react"
import { Eye, EyeOff } from "lucide-react"

import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// Password field with a show/hide toggle. Drop-in replacement for <Input> on
// password fields — forwards all input props; the `type` is managed internally.
function PasswordInput({
  className,
  ...props
}: Omit<React.ComponentProps<typeof Input>, "type">) {
  const [visible, setVisible] = React.useState(false)
  return (
    <div className="relative">
      <Input
        type={visible ? "text" : "password"}
        // [&::-ms-reveal]:hidden suppresses Edge's built-in reveal eye.
        // [&::-webkit-credentials-auto-fill-button]:hidden and the strong-password
        // variant suppress Chrome/Safari's native password affordance icons so they
        // don't appear alongside our own custom toggle.
        className={cn(
          "pr-9 [&::-ms-reveal]:hidden [&::-webkit-credentials-auto-fill-button]:hidden [&::-webkit-strong-password-auto-fill-button]:hidden",
          className,
        )}
        {...props}
      />
      <button
        type="button"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        className="absolute inset-y-0 right-0 flex items-center px-2.5 text-muted-foreground transition-colors hover:text-foreground focus-visible:text-foreground focus-visible:outline-none"
      >
        {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </button>
    </div>
  )
}

export { PasswordInput }
