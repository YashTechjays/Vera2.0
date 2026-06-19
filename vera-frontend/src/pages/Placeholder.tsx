import { Construction } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"

type PlaceholderProps = {
  title: string
}

export function Placeholder({ title }: PlaceholderProps) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="text-sm text-muted-foreground">
          This section is a work in progress.
        </p>
      </div>

      <Card>
        <CardContent className="flex flex-col items-center justify-center gap-3 py-16 text-center">
          <Construction className="size-10 text-muted-foreground" />
          <div>
            <p className="font-medium">Nothing here yet</p>
            <p className="text-sm text-muted-foreground">
              The {title} view will be built out soon.
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
