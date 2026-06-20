/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the control-plane API, including the /api/v1 prefix. */
  readonly VITE_API_BASE_URL?: string
  /** Dev convenience: tenant slug (workspace handle) prefilled into the login form. */
  readonly VITE_DEFAULT_TENANT_SLUG?: string
  /** TEMP / dev-only: prefill login email. Remove before non-local use. */
  readonly VITE_DEV_EMAIL?: string
  /** TEMP / dev-only: prefill login password. Remove before non-local use. */
  readonly VITE_DEV_PASSWORD?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
