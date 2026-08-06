import { describe, expect, it } from "vitest"

import type { TenantDetail } from "@/lib/api/platform"
import { changedTenantFields, isValidSlug, slugify } from "./platformTenants.helpers"

describe("isValidSlug", () => {
  it("accepts a lowercase DNS label", () => {
    expect(isValidSlug("acme")).toBe(true)
    expect(isValidSlug("acme-health-2")).toBe(true)
    expect(isValidSlug("a")).toBe(true)
  })

  it("rejects uppercase, spaces, and underscores", () => {
    expect(isValidSlug("Acme")).toBe(false)
    expect(isValidSlug("acme health")).toBe(false)
    expect(isValidSlug("acme_health")).toBe(false)
  })

  it("rejects leading or trailing hyphens", () => {
    expect(isValidSlug("-acme")).toBe(false)
    expect(isValidSlug("acme-")).toBe(false)
  })

  it("rejects empty and over-63-character slugs", () => {
    expect(isValidSlug("")).toBe(false)
    expect(isValidSlug("a".repeat(63))).toBe(true)
    expect(isValidSlug("a".repeat(64))).toBe(false)
  })
})

describe("slugify", () => {
  it("derives a slug from an organisation name", () => {
    expect(slugify("Acme Health")).toBe("acme-health")
    expect(slugify("St. Mary's Hospital")).toBe("st-mary-s-hospital")
  })

  it("collapses runs of separators and trims the edges", () => {
    expect(slugify("  Acme   --  Health  ")).toBe("acme-health")
    expect(slugify("!!!Acme!!!")).toBe("acme")
  })

  it("caps at 63 characters without a trailing hyphen", () => {
    const slug = slugify("a".repeat(70))
    expect(slug).toHaveLength(63)
    expect(slug.endsWith("-")).toBe(false)
  })

  it("produces a valid slug or an empty string for unusable input", () => {
    expect(slugify("!!!")).toBe("")
    expect(isValidSlug(slugify("Acme Health"))).toBe(true)
  })
})

const tenant: TenantDetail = {
  id: "t1",
  name: "Acme",
  slug: "acme",
  status: "active",
  region: "us-east",
  created_at: "2026-07-30T00:00:00Z",
  observer_enabled: true,
  auto_retry_enabled: false,
  retry_fill_threshold: 0.5,
  max_agents_per_va: 3,
  max_concurrent_calls: 25,
  max_retries: 5,
  queue_expiry_hours: 48,
  recording_retention_days: null,
}

describe("changedTenantFields", () => {
  it("returns nothing when the form matches the tenant", () => {
    expect(changedTenantFields(tenant, { ...tenant })).toEqual({})
  })

  it("returns only the fields that actually changed", () => {
    const patch = changedTenantFields(tenant, { ...tenant, name: "Acme Two", max_retries: 2 })
    expect(patch).toEqual({ name: "Acme Two", max_retries: 2 })
  })

  it("treats a cleared region as an explicit null", () => {
    expect(changedTenantFields(tenant, { ...tenant, region: null })).toEqual({ region: null })
  })

  it("never includes slug or status even when they differ", () => {
    const patch = changedTenantFields(tenant, {
      ...tenant,
      slug: "renamed",
      status: "deactivated",
      name: "Kept",
    })
    expect(patch).toEqual({ name: "Kept" })
  })

  it("detects a false boolean as a change from true", () => {
    expect(changedTenantFields(tenant, { ...tenant, observer_enabled: false })).toEqual({
      observer_enabled: false,
    })
  })
})
