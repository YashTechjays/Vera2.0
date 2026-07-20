import { afterEach, describe, expect, it, vi } from "vitest"

import {
  MAX_ITEMS,
  addItem,
  compareEntryIds,
  isUnread,
  latestEntryId,
  loadReadCursor,
  saveReadCursor,
  shortCallRef,
  timeAgo,
  unreadCount,
  type NotificationItem,
} from "@/lib/notifications/store"

function item(id: string, ts = 1): NotificationItem {
  return { id, callId: "c1", flag: "conversation_loop", score: 30, ts }
}

describe("compareEntryIds", () => {
  it("orders by timestamp then sequence", () => {
    expect(compareEntryIds("100-0", "100-0")).toBe(0)
    expect(compareEntryIds("100-1", "100-0")).toBeGreaterThan(0)
    expect(compareEntryIds("99-9", "100-0")).toBeLessThan(0)
    expect(compareEntryIds("1784351200443-0", "1784351154498-0")).toBeGreaterThan(0)
  })
})

describe("unread state", () => {
  it("everything is unread without a cursor", () => {
    expect(isUnread(item("5-0"), null)).toBe(true)
  })

  it("cursor splits read from unread", () => {
    const items = [item("7-0"), item("6-0"), item("5-0")]
    expect(unreadCount(items, "6-0")).toBe(1) // only 7-0 is newer
    expect(isUnread(item("6-0"), "6-0")).toBe(false) // at-cursor = read
  })
})

describe("addItem", () => {
  it("prepends newest and dedupes by id", () => {
    const one = addItem([], item("1-0"))
    const two = addItem(one, item("2-0"))
    expect(two.map((i) => i.id)).toEqual(["2-0", "1-0"])
    expect(addItem(two, item("2-0"))).toBe(two) // replayed id — unchanged
  })

  it("caps the list", () => {
    let items: NotificationItem[] = []
    for (let i = 0; i < MAX_ITEMS + 5; i++) items = addItem(items, item(`${i}-0`))
    expect(items).toHaveLength(MAX_ITEMS)
    expect(items[0].id).toBe(`${MAX_ITEMS + 4}-0`) // newest survives
  })
})

describe("latestEntryId", () => {
  it("finds the chronologically newest id regardless of order", () => {
    expect(latestEntryId([])).toBeNull()
    expect(latestEntryId([item("5-0"), item("9-1"), item("9-0")])).toBe("9-1")
  })
})

describe("timeAgo", () => {
  it("buckets ages", () => {
    const now = 1_000_000_000
    expect(timeAgo(now - 30_000, now)).toBe("just now")
    expect(timeAgo(now - 5 * 60_000, now)).toBe("5m ago")
    expect(timeAgo(now - 3 * 3_600_000, now)).toBe("3h ago")
    expect(timeAgo(now - 2 * 86_400_000, now)).toBe("2d ago")
  })
})

describe("read cursor persistence", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("round-trips through sessionStorage", () => {
    const backing = new Map<string, string>()
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => backing.get(k) ?? null,
      setItem: (k: string, v: string) => void backing.set(k, v),
    })
    expect(loadReadCursor()).toBeNull()
    saveReadCursor("123-0")
    expect(loadReadCursor()).toBe("123-0")
  })

  it("degrades quietly when storage is unavailable", () => {
    vi.stubGlobal("sessionStorage", undefined)
    expect(loadReadCursor()).toBeNull()
    expect(() => saveReadCursor("1-0")).not.toThrow()
  })
})

describe("shortCallRef", () => {
  it("takes the last 8 hex characters, uppercased, hash-prefixed", () => {
    expect(shortCallRef("9f4f7b10-1cf7-4c5b-8ff6-46d2e8b06e57")).toBe("#E8B06E57")
  })

  it("ignores dashes when counting the last 8 characters", () => {
    // last 8 chars of the compacted (dash-free) id, not the raw string's tail
    expect(shortCallRef("aaaaaaaa-bbbb-cccc-dddd-ee0000000000")).toBe("#00000000")
  })

  it("disambiguates two different calls", () => {
    const a = shortCallRef("9f4f7b10-1cf7-4c5b-8ff6-46d2e8b06e57")
    const b = shortCallRef("7c1e0000-0000-4000-8000-000000000003")
    expect(a).not.toBe(b)
    expect(a).toMatch(/^#[0-9A-F]{8}$/)
    expect(b).toMatch(/^#[0-9A-F]{8}$/)
  })

  it("is a pure function of the id (same id -> same ref)", () => {
    expect(shortCallRef("abc-123")).toBe(shortCallRef("abc-123"))
  })
})
