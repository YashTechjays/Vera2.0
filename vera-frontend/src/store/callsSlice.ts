import { createAsyncThunk, createSlice, type PayloadAction } from "@reduxjs/toolkit"

import * as callsApi from "@/lib/api/calls"
import type { CallSummary } from "@/lib/api/calls"
import { ApiError } from "@/lib/api/client"

type CallsState = {
  items: CallSummary[]
  loading: boolean
  error: string | null
}

const initialState: CallsState = { items: [], loading: false, error: null }

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}

/** Load the active worklist (owner-or-published). Polled by Live Monitoring. */
export const fetchCalls = createAsyncThunk("calls/fetch", async () => {
  return await callsApi.listCalls()
})

/** Publish a call the current user owns; the fulfilled payload replaces the row. */
export const publishCall = createAsyncThunk("calls/publish", async (callId: string) => {
  return await callsApi.publishCall(callId)
})

const callsSlice = createSlice({
  name: "calls",
  initialState,
  reducers: {
    clearCallsError(state) {
      state.error = null
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchCalls.pending, (s) => {
        s.loading = true
        s.error = null
      })
      .addCase(fetchCalls.fulfilled, (s, a: PayloadAction<CallSummary[]>) => {
        s.loading = false
        s.items = a.payload
      })
      .addCase(fetchCalls.rejected, (s, a) => {
        s.loading = false
        s.error = message(a.error, "Could not load calls.")
      })
      // Reflect the publish immediately by swapping the updated row in place, so the
      // "Visible To All" state flips without waiting for the next poll.
      .addCase(publishCall.fulfilled, (s, a: PayloadAction<CallSummary>) => {
        const i = s.items.findIndex((c) => c.id === a.payload.id)
        if (i !== -1) s.items[i] = a.payload
      })
      .addCase(publishCall.rejected, (s, a) => {
        s.error = message(a.error, "Could not publish the call.")
      })
  },
})

export const { clearCallsError } = callsSlice.actions

export default callsSlice.reducer

export const selectActiveCalls = (s: { calls: CallsState }) => s.calls.items
export const selectCallsLoading = (s: { calls: CallsState }) => s.calls.loading
export const selectCallsError = (s: { calls: CallsState }) => s.calls.error
