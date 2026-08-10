# Interventions per day — stacked chart redesign

**Date:** 2026-08-06
**Status:** approved (in-conversation)
**Scope:** replace the "Interventions by type" totals bar chart in the History Report
with a per-day stacked bar chart.

## Problem

The current chart (`vera-frontend/src/components/analytics/HistoryReport.tsx`) is a
horizontal totals-only bar chart: single flat teal, raw lowercase enum labels, no value
labels, no sorting. With only four intervention types it reads as a few fat bars in
empty space, and it answers a weaker question (which type dominates) than the one a
supervisor actually asks (when did interventions spike, and which type drove the spike).

## Design

### Backend — `GET /api/v1/analytics/report`

Add an `interventions_per_day` series to `HistoryReport`
(`control_plane/api/v1/analytics.py`):

```
interventions_per_day: [{ day, flag, coach, whisper, takeover }]
```

- One row per UTC day that has at least one intervention in the current window; the
  four counts are per `InterventionType`, zero-filled.
- Day bucket = the **call's** `created_at` day (same `date_trunc` expression and
  window conds as `calls_per_day`), so an intervention belongs to its call's day and
  all report series share one time convention.
- Same GROUP BY day+type query shape as the existing totals; same provider/VA filters.
- `interventions_by_type` (totals) stays in the response — existing consumers keep
  working; only the frontend chart stops reading it.

### Frontend — `HistoryReport.tsx`

Replace the horizontal bar chart with a stacked vertical bar chart titled
"Interventions per day":

- X axis: UTC day buckets — the union of days from `calls_per_day` and
  `interventions_per_day` (sorted), zero-filled, so the two charts share one axis and
  a day with calls but no interventions still appears (as an empty slot).
- One `<Bar stackId>` per type, consistent color per type, Title Case labels
  (Flag / Coach / Whisper / Takeover) in legend and tooltip.
- Tooltip: per-type breakdown for the day.
- Empty state: when the range has no interventions at all, render
  "No interventions in this period" instead of an empty plot.
- Day-merge logic lives in `lib/analytics/report.ts` as a pure helper with unit tests.

## Testing

- Backend: extend `tests/integration/control_plane/test_analytics.py` — interventions
  of two types on calls on different days assert the per-day rows, zero-fill, and
  filter behavior.
- Frontend: unit-test the day-merge helper; extend `HistoryReport.test.tsx` for the
  new chart title and empty state.

## Out of scope

- Zero-filling days with no calls at all (the existing `calls_per_day` chart already
  omits them; changing that convention is a separate decision).
- Any change to the metric cards or other report fields.
