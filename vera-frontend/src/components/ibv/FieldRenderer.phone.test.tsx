import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { LeafField } from "@/lib/ibv/types"
import { FieldRenderer } from "./FieldRenderer"

const phoneField: LeafField = {
  type: "phone",
  title: "Insurance Provider Phone",
} as LeafField

function renderPhone(value: string, onChange = vi.fn()) {
  render(
    <FieldRenderer
      field={phoneField}
      path="sections.insurance_reference_information.insurance_phone_number"
      value={value}
      onChange={onChange}
      countrySelect
    />,
  )
  return onChange
}

describe("FieldRenderer phone country select", () => {
  it("renders a country dropdown plus the number input", () => {
    renderPhone("+12125551234")
    expect(screen.getByRole("combobox", { name: "Country code" })).toHaveValue("US")
    expect(screen.getByRole("textbox")).toHaveValue("2125551234")
  })

  it("preselects the country the value belongs to", () => {
    renderPhone("+919876543210")
    expect(screen.getByRole("combobox", { name: "Country code" })).toHaveValue("IN")
    expect(screen.getByRole("textbox")).toHaveValue("9876543210")
  })

  it("composes E.164 from typed digits with the selected country", async () => {
    const user = userEvent.setup()
    const onChange = renderPhone("")
    await user.type(screen.getByRole("textbox"), "2")
    expect(onChange).toHaveBeenLastCalledWith("+12")
  })

  it("recomposes when the country changes", async () => {
    const user = userEvent.setup()
    const onChange = renderPhone("+12125551234")
    await user.selectOptions(screen.getByRole("combobox", { name: "Country code" }), "IN")
    expect(onChange).toHaveBeenLastCalledWith("+912125551234")
  })

  it("clears the value when the digits are emptied", async () => {
    const user = userEvent.setup()
    const onChange = renderPhone("+12")
    await user.clear(screen.getByRole("textbox"))
    expect(onChange).toHaveBeenLastCalledWith("")
  })

  it("keeps the plain input without countrySelect", () => {
    render(
      <FieldRenderer
        field={phoneField}
        path="sections.verification_information.callback_number"
        value="N/A"
        onChange={vi.fn()}
      />,
    )
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument()
  })
})
