import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { MetricCard } from "@/components/analytics/MetricCard"

function trendLine(): HTMLElement {
  return screen.getByText(/vs previous period/).closest("p") as HTMLElement
}

describe("MetricCard trend indicator (VR2-283)", () => {
  it("zero change is neutral: flat icon, muted color, no arrow", () => {
    render(<MetricCard label="Intervention Rate" value="25.0%" deltaPct={0} invert />)
    expect(screen.getByLabelText("flat")).toBeInTheDocument()
    expect(screen.queryByLabelText("up")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("down")).not.toBeInTheDocument()
    expect(trendLine().className).toContain("text-muted-foreground")
  })

  it("a delta that merely rounds to 0.0% is also neutral", () => {
    render(<MetricCard label="Call Volume" value="40" deltaPct={-0.04} />)
    expect(screen.getByText(/0\.0% vs previous period/)).toBeInTheDocument()
    expect(screen.getByLabelText("flat")).toBeInTheDocument()
  })

  it("an increase is a green up-arrow on a normal metric", () => {
    render(<MetricCard label="Call Volume" value="40" deltaPct={12.5} />)
    expect(screen.getByLabelText("up")).toBeInTheDocument()
    expect(trendLine().className).toContain("text-emerald-600")
  })

  it("an increase is a red up-arrow on an inverted metric (more interventions = bad)", () => {
    render(<MetricCard label="Intervention Rate" value="25.0%" deltaPct={12.5} invert />)
    expect(screen.getByLabelText("up")).toBeInTheDocument()
    expect(trendLine().className).toContain("text-red-600")
  })

  it("a decrease is a green down-arrow on an inverted metric", () => {
    render(<MetricCard label="Intervention Rate" value="25.0%" deltaPct={-10} invert />)
    expect(screen.getByLabelText("down")).toBeInTheDocument()
    expect(trendLine().className).toContain("text-emerald-600")
  })

  it("renders no trend line when the delta is unknown", () => {
    render(<MetricCard label="Call Volume" value="40" deltaPct={null} />)
    expect(screen.queryByText(/vs previous period/)).not.toBeInTheDocument()
  })
})
