import { configureStore } from "@reduxjs/toolkit"
import authReducer from "@/store/authSlice"

export const store = configureStore({
  reducer: { auth: authReducer },
})

export type RootState = ReturnType<typeof store.getState>
export type AppDispatch = typeof store.dispatch

import { registerAuthFailureHandler } from "@/lib/api/client"
import { forceLogout } from "@/store/authSlice"

registerAuthFailureHandler(() => store.dispatch(forceLogout()))
