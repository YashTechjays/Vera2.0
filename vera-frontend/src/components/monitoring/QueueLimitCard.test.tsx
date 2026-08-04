import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { QueueLimitCard } from "@/components/monitoring/QueueLimitCard"

describe("QueueLimitCard", () => {
  it("shows limit, active, and in-queue counts", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 3, in_queue: 2 }} />)
    expect(screen.getByText("Active Call Queue Limit")).toBeInTheDocument()
    expect(screen.getByText("Active")).toBeInTheDocument()
    expect(screen.getByText("In Queue")).toBeInTheDocument()
    expect(screen.getByText("2")).toBeInTheDocument()
  })

  it("explains the wait when the limit is reached and calls are queued", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 3, in_queue: 2 }} />)
    expect(screen.getByText(/queued calls start when a slot frees up/i)).toBeInTheDocument()
  })

  it("stays quiet below the limit", () => {
    render(<QueueLimitCard status={{ limit: 3, active: 1, in_queue: 0 }} />)
    expect(screen.queryByText(/slot frees up/i)).not.toBeInTheDocument()
  })

  it("renders nothing while the status is loading", () => {
    const { container } = render(<QueueLimitCard status={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})
