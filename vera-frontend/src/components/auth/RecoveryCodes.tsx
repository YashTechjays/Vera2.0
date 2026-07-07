import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { useCopy } from "@/lib/clipboard"

export function RecoveryCodes({ codes, onContinue }: { codes: string[]; onContinue: () => void }) {
  const [saved, setSaved] = useState(false)
  const { state: copyState, copy } = useCopy()
  function download() {
    const blob = new Blob([codes.join("\n")], { type: "text/plain" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = "vera-recovery-codes.txt"
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
        Save these recovery codes now. Each works once if you lose your authenticator.
        They will not be shown again.
      </div>
      <div className="grid grid-cols-2 gap-2 rounded-md border bg-muted/30 p-3 font-mono text-sm">
        {codes.map((c, i) => <span key={i}>{c}</span>)}
      </div>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => copy(codes.join("\n"))}>
          {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy"}
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={download}>Download</Button>
      </div>
      <div className="flex items-center gap-2 text-sm">
        <Checkbox id="saved-codes" checked={saved} onCheckedChange={(checked) => setSaved(checked === true)} />
        <Label htmlFor="saved-codes">I have saved my recovery codes</Label>
      </div>
      <Button className="w-full" disabled={!saved} onClick={onContinue}>Continue</Button>
    </div>
  )
}
