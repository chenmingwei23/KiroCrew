"use strict";
//
// Blank-dashboard self-healing for the main window, from the MAIN PROCESS.
//
// The problem this solves: the desktop app can open to a blank white screen
// after running fine for days — seen on Windows. The dashboard document loads
// (its HTML arrives, `did-finish-load` fires), but the SPA never paints: a
// hashed entry chunk the webview is replaying from its HTTP cache fails to
// evaluate, the entry `<script type=module>` dies silently, and nothing of ours
// ever runs in that frame. A reopen does not fix it and an ordinary in-app
// reload does not either (a plain reload is cache-permitted, so it re-reads the
// same bytes); only a cache-bypassing hard reload (Ctrl+Shift+R) recovers it.
//
// Why this lives in the MAIN process, not the bundle: the dashboard's own
// boot-time code cannot heal a window where that code never ran. A client-side
// check ships inside the very bundle that failed to evaluate, so on the real
// blank screen it never executes. The main process, by contrast, sees
// `did-finish-load` regardless of whether any renderer JavaScript ran, and
// `webContents.reloadIgnoringCache()` is the literal Ctrl+Shift+R — it bypasses
// the HTTP cache so the reload re-fetches a fresh shell, and it works even when
// the frame is running no code at all.
//
// Mechanism, and why each part is shaped the way it is:
//   1. On a top-level `did-finish-load` at the dashboard's OWN origin, arm a
//      timer. Only the dashboard origin counts: the boot splash (`loading.html`)
//      and any remote-crew pane load must not start or cancel this watch.
//   2. A healthy boot cancels the timer. The dashboard renderer sends one
//      `dashboard:booted` ping from its mount (see website/src/main.tsx); the
//      main process relays it here via `noteBooted()`. A frame that painted and
//      ran its bundle is not the failure this heals.
//   3. If the timer fires first — loaded, but no boot within the window — call
//      `reloadIgnoringCache()` ONCE. Bounded by `maxAttempts` within `windowMs`
//      exactly like renderer-recovery.js: a genuinely broken build that can
//      never boot would otherwise reload forever, spinning CPU and hiding the
//      failure. After the budget is spent the watch goes quiet and reports, so
//      the blank window surfaces instead of looping.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so every decision is testable without a live BrowserWindow
// (same pattern as renderer-recovery.js / gpu-crash-fallback.js).
//

// How long after the dashboard document finishes loading we wait for the
// `dashboard:booted` ping before deciding the shell is stuck. Generous: a cold
// start over a slow link, or a large session restoring, legitimately takes many
// seconds to mount, and a false reload throws away a load that would have
// arrived. The failure this heals is permanent (a cache capsule never boots),
// so waiting longer costs only latency on a window that is already blank.
const DEFAULT_BOOT_TIMEOUT_MS = 20_000;

// At most this many cache-ignoring reloads within the sliding window. One is
// almost always enough (a fresh shell boots); the cap stops a build that can
// never boot from reloading forever.
const DEFAULT_MAX_ATTEMPTS = 2;
const DEFAULT_WINDOW_MS = 10 * 60_000;

/**
 * Whether `url` is a top-level document at the dashboard's own origin. Origin
 * comparison, not prefix: `http://127.0.0.1:5476` must not match
 * `http://127.0.0.1:54760`, and the splash/pane documents at other origins are
 * excluded. A malformed URL (or a mismatched backend) reads as "not the
 * dashboard", so the watch simply never arms rather than arming on guesswork.
 */
function isDashboardDocument(url, backendUrl) {
  try {
    return new URL(String(url)).origin === new URL(String(backendUrl)).origin;
  } catch {
    return false;
  }
}

/**
 * Create the blank-dashboard watchdog.
 *
 * @param {object} deps
 * @param {string} deps.backendUrl        The dashboard origin; only a load of a
 *   document here arms the watch.
 * @param {() => void} deps.reloadIgnoringCache  Force a cache-bypassing reload
 *   of the dashboard window (the literal Ctrl+Shift+R).
 * @param {(fn: () => void, ms: number) => any} [deps.setTimer]
 * @param {(t: any) => void} [deps.clearTimer]
 * @param {() => boolean} [deps.isQuitting]
 * @param {(msg: string) => void} [deps.log]
 * @param {(info: object) => void} [deps.onGiveUp] Called once the budget is spent.
 * @param {() => number} [deps.now]
 * @param {number} [deps.bootTimeoutMs]
 * @param {number} [deps.maxAttempts]
 * @param {number} [deps.windowMs]
 * @returns {{
 *   noteDocumentLoaded: (url: string) => void,
 *   noteBooted: () => void,
 *   fireForTest: () => string,
 *   reset: () => void,
 *   armed: boolean,
 *   attempts: number,
 * }}
 */
function createStaleShellWatchdog({
  backendUrl,
  reloadIgnoringCache,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (t) => clearTimeout(t),
  isQuitting = () => false,
  log = () => {},
  onGiveUp = () => {},
  now = () => Date.now(),
  bootTimeoutMs = DEFAULT_BOOT_TIMEOUT_MS,
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  windowMs = DEFAULT_WINDOW_MS,
} = {}) {
  const timeout = Math.max(1, Number(bootTimeoutMs) || DEFAULT_BOOT_TIMEOUT_MS);
  const cap = Math.max(1, Number(maxAttempts) || DEFAULT_MAX_ATTEMPTS);
  const span = Math.max(1, Number(windowMs) || DEFAULT_WINDOW_MS);

  // The armed timer handle, or null when nothing is pending.
  let timer = null;
  // Timestamps of recent cache-ignoring reloads, pruned to the sliding window
  // so a stable app that hits this once a day never exhausts its budget.
  let recent = [];

  function disarm() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
  }

  /** The timer fired: the dashboard loaded but never announced a healthy boot. */
  function onBootTimeout() {
    timer = null;
    if (isQuitting()) {
      log("stale-shell watchdog: boot timed out during quit — not reloading");
      return "ignored-quitting";
    }
    const t = now();
    recent = recent.filter((ts) => t - ts < span);
    if (recent.length >= cap) {
      log(
        `stale-shell watchdog: dashboard loaded but did not boot, and ${recent.length} ` +
          `cache-ignoring reload(s) within ${span}ms already did not help — ` +
          `giving up to avoid a reload loop`,
      );
      onGiveUp({ attempts: recent.length });
      return "gave-up";
    }
    recent.push(t);
    log(
      `stale-shell watchdog: dashboard document loaded but no boot within ${timeout}ms — ` +
        `forcing a cache-ignoring reload (attempt ${recent.length}/${cap})`,
    );
    try {
      reloadIgnoringCache();
    } catch (e) {
      // Never let a failed reload escape into Electron's event emitter.
      log(`stale-shell watchdog: reloadIgnoringCache failed: ${e && e.message}`);
    }
    return "reloaded";
  }

  /**
   * A top-level document finished loading. Arm the watch only for the
   * dashboard's own origin; a splash or pane load is ignored. Re-arming on a
   * fresh dashboard load (including the reload this watch itself triggered)
   * replaces any pending timer — the new load gets its own full window.
   */
  function noteDocumentLoaded(url) {
    if (!isDashboardDocument(url, backendUrl)) return;
    disarm();
    timer = setTimer(onBootTimeout, timeout);
  }

  /**
   * The dashboard SPA announced a healthy boot (`dashboard:booted`). Cancel the
   * pending watch — the frame painted and ran its bundle, so it is not the
   * blank-shell failure. Harmless if nothing is armed (a boot with no prior
   * dashboard load, or a duplicate ping).
   */
  function noteBooted() {
    disarm();
  }

  return {
    noteDocumentLoaded,
    noteBooted,
    // Test hook: run the timeout decision synchronously without waiting.
    fireForTest: onBootTimeout,
    reset() {
      disarm();
      recent = [];
    },
    get armed() {
      return timer !== null;
    },
    get attempts() {
      return recent.length;
    },
  };
}

module.exports = {
  DEFAULT_BOOT_TIMEOUT_MS,
  DEFAULT_MAX_ATTEMPTS,
  DEFAULT_WINDOW_MS,
  isDashboardDocument,
  createStaleShellWatchdog,
};
