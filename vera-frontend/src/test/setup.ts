// Vitest global setup: registers @testing-library/jest-dom matchers
// (toBeInTheDocument, toHaveValue, ...) on vitest's `expect`.
import "@testing-library/jest-dom/vitest"

import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"

// No `test.globals` in vitest.config, so RTL's auto-cleanup (which detects a
// global `afterEach`) never registers itself — do it explicitly, or renders
// from one test bleed into the next test's DOM.
afterEach(cleanup)
