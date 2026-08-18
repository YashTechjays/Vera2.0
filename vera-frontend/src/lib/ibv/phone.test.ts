import { describe, expect, it } from "vitest"

import type { FormSchema } from "./types"
import { composePhoneValue, dialedPhonePath } from "./phone"

const PHONE_PATH = "sections.insurance_reference_information.insurance_phone_number"

const schema = {
  dsl_version: "2.1",
  name: "Test Form",
  sections: {
    insurance_reference_information: {
      title: "Insurance Reference Information",
      fields: {
        insurance_phone_number: { type: "phone", title: "Insurance Provider Phone" },
      },
    },
    verification_information: {
      title: "Verification Information",
      fields: {
        callback_number: { type: "phone", title: "Callback Number", default: "N/A" },
      },
    },
  },
  system_fields: {
    insurance_provider_phone_number: PHONE_PATH,
    callback_number: "sections.verification_information.callback_number",
  },
} as unknown as FormSchema

describe("dialedPhonePath", () => {
  it("resolves only the insurance_provider_phone_number handle, not other phone leaves", () => {
    expect(dialedPhonePath(schema)).toBe(PHONE_PATH)
  })

  it("is undefined when the schema has no such handle", () => {
    expect(dialedPhonePath({ ...schema, system_fields: {} } as FormSchema)).toBeUndefined()
  })
})

describe("composePhoneValue", () => {
  it("joins country calling code and digits into E.164", () => {
    expect(composePhoneValue("US", "2125551234")).toBe("+12125551234")
    expect(composePhoneValue("IN", "9876543210")).toBe("+919876543210")
  })

  it("strips separators from the national part", () => {
    expect(composePhoneValue("US", "212-555 1234")).toBe("+12125551234")
  })

  it("is empty when there are no digits", () => {
    expect(composePhoneValue("US", "")).toBe("")
    expect(composePhoneValue("US", "- ")).toBe("")
  })
})
