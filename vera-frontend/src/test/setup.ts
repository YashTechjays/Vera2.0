// Vitest global setup: registers @testing-library/jest-dom matchers
// (toBeInTheDocument, toHaveValue, ...) on vitest's `expect`.
import "@testing-library/jest-dom/vitest"

import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"

// No `test.globals` in vitest.config, so RTL's auto-cleanup (which detects a
// global `afterEach`) never registers itself — do it explicitly, or renders
// from one test bleed into the next test's DOM.
afterEach(cleanup)

// jsdom ships no ResizeObserver, and Radix primitives that measure themselves
// (Checkbox, Select, ...) construct one on mount — without this, rendering them
// throws "ResizeObserver is not defined". Never fires in jsdom; a stub is enough.
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

// Radix Select's open/close pointer handling and item scrolling touch two more
// APIs jsdom lacks — same never-fires-in-jsdom reasoning as ResizeObserver above.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
