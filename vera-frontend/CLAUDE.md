# Vera 2.0 frontend — PHI / HIPAA guardrails

Vera is a HIPAA-regulated, multi-tenant AI voice platform. This repo is a
**React + Vite + TypeScript** app (React Router, shadcn / Radix UI, Tailwind, Zod) —
these guardrails apply to all UI code, so write to them from commit one. The backend
(`vera-backend`) enforces the server side; the browser is **outside** the trust
boundary and is the last place PHI can leak.

## What the browser may and may not receive

- The browser receives **plaintext PHI over TLS only**, inside an authorized, authenticated
  session. Display it, then discard it.
- The server returns already-decrypted, minimized plaintext (PHI at rest is protected by
  Google CMEK at the storage layer). The browser does no decryption and holds no keys.

## Holding PHI

- Hold PHI in **session-scoped component state only**. NEVER persist it in `localStorage`,
  `sessionStorage`, `IndexedDB`, or cookies.
- Short idle timeout on sensitive views; re-authenticate before re-showing PHI.

## PHI never in URLs

- No PHI in URLs, paths, query strings, route params, or fragments — identifiers are opaque
  UUIDs. URLs end up in history, `Referer` headers, and server logs.

## Rendering & transport

- Strict Content-Security-Policy. Rely on the framework's output escaping; no
  `dangerouslySetInnerHTML` (or any raw-HTML injection) anywhere near PHI.
- Honor `Cache-Control: no-store` on PHI responses — do not cache them in a service worker
  or client-side cache.

## Telemetry

- Exclude every PHI surface from Sentry, session-replay, analytics, and any non-BAA SaaS.
  Scrub PHI from breadcrumbs and error payloads before they leave the browser. Never
  `console.log` a raw identifier.

## Crypto

- Do NOT introduce client-held-key or end-to-end encryption unless a written requirement
  forces it. The default model is server-side handling + Google CMEK at rest + TLS in transit;
  a client-held key usually adds risk (key handling in the browser) without moving the trust
  boundary.

## When in doubt, stop and ask

A blocked task is recoverable; a PHI disclosure is not. When you cannot tell whether
something is PHI, treat it as PHI, and defer the call to compliance review.

## Dependency changes

- The npm version is pinned via the `packageManager` field in `package.json` and enforced
  by Corepack (run `corepack enable` once per machine). Never install/update a global npm
  and regenerate the lock file with it — a different npm version can resolve the same
  `package.json` into a different `package-lock.json` (observed: optional peer deps of
  `@napi-rs/wasm-runtime` hoisted differently between npm 11 and CI's npm 10.9.8), and
  `npm ci` in CI will reject the drifted lock file with `EUSAGE`.
- After touching `package.json` or `package-lock.json`, verify with `npm ci` (not just
  `npm install`) before pushing — `npm ci` is the strict check CI runs; `npm install` will
  silently "fix" a lockfile locally without surfacing that it drifted from what CI expects.
