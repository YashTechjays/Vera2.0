# Live Monitoring pagination (VR2-160) — design

Ticket: https://veratechsolutions.atlassian.net/browse/VR2-160
Branch: `fix/live-monitoring-pagination` (off `origin/dev`)

## Problem

The Live Monitoring page renders every call in one unbounded list. Two distinct
defects hide behind that:

- **Active / Critical tabs** poll `GET /calls` (scope `live`) every 8 s. The
  endpoint is deliberately unbounded for live calls, so the table grows without
  a paging control.
- **Completed tab** calls `GET /calls?scope=history`, which the server silently
  caps at the 50 newest terminal calls — anything older is unreachable from
  this page no matter what the UI does.

## Approach (decided)

Hybrid — chosen over client-only (leaves history >cap invisible) and
server-side-everywhere (breaks the notification deep-link and SSE-ended
pinning, which assume the full live list is in memory, and lets the stat cards
disagree with the table):

- **Completed tab → server-side pagination**, reusing the envelope
  `GET /call-history` already returns: `{items, page, page_size, total}`.
- **Active / Critical tabs → client-side pagination**: keep polling the full
  live list; slice the current page locally.

## Backend

`GET /calls` (`apps/control_plane/src/control_plane/api/v1/calls.py::list_calls`):

- Add `page` (ge 1, default 1) and `page_size` (ge 1, le 100, default 20)
  query params — honored only for `scope=history`.
- `scope=history` responds with the paginated envelope
  `{items: CallSummary[], page, page_size, total}`; `total` comes from a count
  query over the same status + visibility filter. Route `response_model`
  becomes the union of the two payload shapes.
- `scope=live` is untouched: unbounded bare array; the existing `limit` param
  remains live-only (history ignores it — `page`/`page_size` supersede it).
- Ordering (`created_at` desc), `_visible_to` visibility, RLS, the PHI read
  audit, and `Cache-Control: no-store` are all unchanged.
- The frontend is the only consumer of `scope=history`; no compatibility shim.

## Frontend

`src/lib/api/calls.ts`:

- `listCalls()` keeps serving the live scope (unchanged signature/shape).
- New `listCompletedCalls({page, page_size})` → `{items: CallSummary[], page,
  page_size, total}` (reuse/generalize the existing `PaginatedCalls` shape).

`src/pages/LiveMonitoring.tsx`:

- One `page` state; reset to 1 on every tab change. Page size 10 (Azad's
  call during manual testing; Call History keeps its 20).
- Active/Critical: poll unchanged; `rows` slices the current 10-row window
  from the full in-memory list.
- Completed: fetch the current server page; a page change refetches, and the
  8 s poll re-reads the current page.
- Clamp: if the current page falls past the last page (rows ended/moved
  between polls), snap to the last non-empty page (page 1 when empty).
- Pagination control: same look/behavior as the Call History page's
  (prev/next + "Showing X–Y of Z"); extract it into a shared component if it
  is not one already.
- Untouched: stat cards (`/calls/stats`, always full-set counts), notification
  deep-link, SSE-ended pinning, empty state, publish toggle, modal.

## Tests

- Backend (integration, `tests/integration/control_plane/test_calls.py`):
  history page slicing, `total` correctness, newest-first order across pages,
  past-the-end page → empty items with correct total, visibility rules still
  enforced, live scope unchanged.
- Frontend (component): local slicing on Active, page reset on tab switch,
  Completed passes `page` to the API, clamp behavior, control renders counts.
- Gates: backend `just check`; frontend `tsc` + `eslint` + tests + build.

## Out of scope

Filters/search on Live Monitoring, changing the 8 s poll cadence, any change
to `/call-history` or the Call History page, stat-card semantics.
