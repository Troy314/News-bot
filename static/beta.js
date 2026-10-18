// Beta mode: what is being developed AFTER the new visual went live (2026-10-14) is built behind this switch, so it reaches
// nobody until it is judged ready. A general admin opens it from Admin → Bêta ("/?beta=on" in a new tab); it is remembered for
// that tab only (sessionStorage) and sets <html data-beta="on">. "/?beta=off", or the banner's button, turns it off.
//
// How to build a change in beta: client side, branch on `isBeta()` (the old behaviour stays the else-branch) or hang the CSS on
// `html[data-beta="on"]`. When it is approved, delete the switch and keep the beta branch. Only a general admin can open it
// (the Bêta panel is admin-only), but the flag itself is a per-tab preference: never put anything in it that must be secret
// or that the server must refuse — server-side checks stay as they are.
const KEY = "beta";
let forced = false; // storage unavailable (private mode): the choice lasts for this page load

function read() {
  try { return window.sessionStorage.getItem(KEY) === "on"; } catch { return forced; }
}

function write(on) {
  forced = on;
  try {
    if (on) window.sessionStorage.setItem(KEY, "on");
    else window.sessionStorage.removeItem(KEY);
  } catch { /* the choice is then kept in `forced` only */ }
}

function apply(on) {
  if (on) document.documentElement.dataset.beta = "on";
  else delete document.documentElement.dataset.beta;
}

export const isBeta = () => read();

export function setBeta(on) {
  write(on);
  apply(on);
}

/** Call once before the first render: reads `?beta=on|off` (and removes it from the address), applies the choice. */
export function initBeta() {
  const url = new URL(window.location.href);
  const asked = url.searchParams.get("beta");
  if (asked === "on" || asked === "off") {
    write(asked === "on");
    url.searchParams.delete("beta");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }
  apply(read());
}
