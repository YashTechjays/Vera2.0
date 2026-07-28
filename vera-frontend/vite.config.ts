import path from "path"
// vitest/config re-exports Vite's defineConfig with the `test` field typed in.
import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Dev-only: forward same-origin /api calls to the control plane so the browser
  // never makes a cross-origin request (sidesteps CORS). Production serves the
  // API behind its own origin/gateway, not through this proxy.
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
})
