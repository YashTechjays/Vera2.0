import { afterEach, describe, expect, it } from "vitest"

import { showBrowserSessionButton, showFormPicker, showIvrToggle } from "./devFlags"

describe("devFlags", () => {
  afterEach(() => {
    localStorage.removeItem("vera:dev-mode")
    localStorage.removeItem("vera:show-ivr-toggle")
    localStorage.removeItem("vera:show-form-picker")
    localStorage.removeItem("vera:show-browser-session")
    localStorage.removeItem("vera.showBrowserSession")
  })

  it("every flag is off by default", () => {
    expect(showIvrToggle()).toBe(false)
    expect(showFormPicker()).toBe(false)
    expect(showBrowserSessionButton()).toBe(false)
  })

  it("turns on only for the exact string 'true'", () => {
    localStorage.setItem("vera:show-ivr-toggle", "true")
    expect(showIvrToggle()).toBe(true)
    localStorage.setItem("vera:show-ivr-toggle", "1")
    expect(showIvrToggle()).toBe(false)

    localStorage.setItem("vera:show-form-picker", "true")
    expect(showFormPicker()).toBe(true)
  })

  it("vera:dev-mode turns every flag on at once", () => {
    localStorage.setItem("vera:dev-mode", "true")
    expect(showIvrToggle()).toBe(true)
    expect(showFormPicker()).toBe(true)
    expect(showBrowserSessionButton()).toBe(true)
  })

  it("browser-session flag honors both the new key and the legacy one", () => {
    localStorage.setItem("vera:show-browser-session", "true")
    expect(showBrowserSessionButton()).toBe(true)
    localStorage.removeItem("vera:show-browser-session")

    localStorage.setItem("vera.showBrowserSession", "1")
    expect(showBrowserSessionButton()).toBe(true)
  })
})
