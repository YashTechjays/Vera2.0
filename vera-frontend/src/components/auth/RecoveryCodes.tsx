import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { copyText } from "@/lib/clipboard"
import { triggerBlobDownload } from "@/lib/utils"

export function RecoveryCodes({ codes, onContinue }: { codes: string[]; onContinue: () => void }) {
  const [saved, setSaved] = useState(false)

  function copy() {
    void copyText(codes.join("\n"))
  }
  function download() {
    triggerBlobDownload(
      new Blob([codes.join("\n")], { type: "text/plain" }),
      "vera-recovery-codes.txt",
    )
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
        <Button type="button" variant="outline" size="sm" onClick={copy}>Copy</Button>
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
