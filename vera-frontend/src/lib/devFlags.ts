// Internal-testing UI flags in localStorage — boolean switches only, never PHI.
// Enable one with `localStorage.setItem("<key>", "true")` in the console + reload;
// removeItem hides it again. Clients never learn the keys, so defaults win.

function readDevFlag(key: string, onValue = "true"): boolean {
  try {
    return localStorage.getItem(key) === onValue
  } catch {
    return false // storage can be blocked (private mode, sandboxed iframe)
  }
}

/** Master switch — `vera:dev-mode` turns every testing control on at once. */
function devMode(): boolean {
  return readDevFlag("vera:dev-mode")
}

/** Bring back the Add Patient Form type picker (step 1 + Back); while hidden, the
 *  modal opens the infertility-treatment form directly.
 *  Key: `vera:show-form-picker` */
export function showFormPicker(): boolean {
  return devMode() || readDevFlag("vera:show-form-picker")
}

/** Show Voice Lab's "Start in-browser session" button.
 *  Key: `vera:show-browser-session` (legacy `vera.showBrowserSession`="1" still works). */
export function showBrowserSessionButton(): boolean {
  return (
    devMode() ||
    readDevFlag("vera:show-browser-session") ||
    readDevFlag("vera.showBrowserSession", "1")
  )
}
