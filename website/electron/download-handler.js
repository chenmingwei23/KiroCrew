"use strict";

const path = require("path");

// Give a renderer-triggered download (`<a download>` + a.click() on a blob URL,
// as the Portability "Download Export (.zip)" button does) a real save path.
//
// The dashboard renders in a WebContentsView under a BaseWindow, which Chromium
// cannot parent a native "Save As" dialog to. With no `will-download` listener
// the DownloadItem waits on that dialog, which never appears, so nothing lands
// on disk while the renderer still reports "Download started" (issue #13047).
// Setting the save path explicitly (OS downloads dir + the item's filename,
// de-duplicated like a browser) lets the download complete with no dialog.
//
// Dependency-injected (`app`, `fs`, `log`) so it unit-tests without a live
// Electron session.

/**
 * Resolve a collision-free absolute save path under `dir` for `filename`.
 *
 * Mirrors the browser's own behaviour: "export.zip", then "export (1).zip",
 * "export (2).zip", ... when earlier ones already exist. Bounded so a wedged
 * filesystem (every candidate "exists") cannot spin forever: when the plain
 * name and every suffix 1..maxTries are already taken, it returns `null` so the
 * caller can refuse the download rather than hand back an occupied path that
 * would overwrite (and destroy) an existing file.
 *
 * A candidate is unavailable if it is already on disk OR present in `reserved`
 * -- the set of paths picked for downloads that are still in flight. Checking
 * only the disk is not enough: two concurrent same-named downloads both start
 * before either file exists, so without the reservation set they would select
 * the same path and one would overwrite the other.
 *
 * @param {{existsSync: (p: string) => boolean}} fs - node `fs` (injectable)
 * @param {string} dir - target directory (absolute)
 * @param {string} filename - the download's own filename
 * @param {number} [maxTries] - collision-suffix ceiling (default 1000)
 * @param {{has: (p: string) => boolean}} [reserved] - paths already claimed by in-flight downloads
 * @returns {string | null} an unused absolute path, or null if none is free
 */
function uniqueSavePath(fs, dir, filename, maxTries = 1000, reserved = null) {
  const ext = path.extname(filename);
  const stem = path.basename(filename, ext);
  const taken = (p) => fs.existsSync(p) || (reserved != null && reserved.has(p));
  let candidate = path.join(dir, filename);
  for (let n = 1; taken(candidate); n += 1) {
    if (n > maxTries) return null;
    candidate = path.join(dir, `${stem} (${n})${ext}`);
  }
  return candidate;
}

/**
 * Build the `will-download` listener for the dashboard session.
 *
 * The returned function has the Electron `(event, item, webContents)` shape and
 * is attached with `session.on("will-download", handler)`. Attach it ONCE per
 * session -- `session.on` adds a listener each call and never removes it, so a
 * per-window attach would stack N handlers for N windows. The caller guards
 * this (see window-lifecycle.js).
 *
 * @param {object} deps
 * @param {{getPath: (name: string) => string}} deps.app - Electron `app`
 * @param {{existsSync: (p: string) => boolean}} [deps.fs] - node `fs` (injectable)
 * @param {(...args: unknown[]) => void} [deps.log] - logger (injectable)
 * @returns {(event: unknown, item: object) => void}
 */
function createWillDownloadHandler({ app, fs = require("fs"), log = () => {} }) {
  // Paths claimed by downloads that are still in flight on this session. The
  // on-disk check alone cannot see a sibling download that has selected a path
  // but not yet written it, so without this two concurrent same-named downloads
  // would pick the same path and one would overwrite the other. One set per
  // handler (one handler per session), released on the item's "done" event.
  const reserved = new Set();

  return function onWillDownload(_event, item) {
    let savePath;
    try {
      const downloads = app.getPath("downloads");
      const filename = item.getFilename() || "download";
      savePath = uniqueSavePath(fs, downloads, filename, 1000, reserved);
      if (!savePath) {
        // Every collision-free name is taken (plain name + suffixes 1..maxTries
        // all exist, on disk or reserved by an in-flight download). Refuse
        // rather than overwrite an existing file: cancel the download and log
        // it. The renderer already reported "Download started", so this is a
        // logged no-op, never silent data loss.
        log(`will-download: no free filename for "${filename}" in ${downloads}; cancelling`);
        if (typeof item.cancel === "function") item.cancel();
        return;
      }
      // Claim the path so a concurrent same-named download selects a different
      // one, and set it so Chromium writes the file instead of waiting for a
      // dialog it cannot parent in this window shape.
      reserved.add(savePath);
      item.setSavePath(savePath);
    } catch (err) {
      // If we cannot even compute a path, let Electron fall back to its own
      // default rather than throwing out of the event handler.
      log("will-download: could not set save path:", err && err.message ? err.message : err);
      return;
    }

    item.once("done", (_doneEvent, state) => {
      // Release the reservation whatever the outcome: a completed download now
      // occupies the path on disk, and a cancelled/interrupted one frees it.
      reserved.delete(savePath);
      if (state !== "completed") {
        // "cancelled" | "interrupted" | anything else: the file did not land.
        log(`will-download: download did not complete (state=${state}) for ${savePath}`);
      }
    });
  };
}

// Sessions already wired with a will-download handler. `session.on` appends a
// listener on every call and never removes it, so attaching per-window would
// stack N handlers (and N `item.once("done")` callbacks) for N windows that
// share the default session, and leak them when a window closes. A WeakSet keys
// off the session object itself, so a session is wired at most once and the
// entry disappears with the session.
const wiredSessions = new WeakSet();

/**
 * Attach the will-download handler to `session` exactly once, no matter how many
 * windows share it. Safe to call from every window's setup path.
 *
 * @param {object} session - the Electron session (view.webContents.session)
 * @param {object} deps - forwarded to {@link createWillDownloadHandler} (app, log, fs)
 * @returns {boolean} true if it attached now, false if this session was already wired
 */
function wireWillDownloadOnce(session, deps) {
  if (!session || wiredSessions.has(session)) return false;
  wiredSessions.add(session);
  session.on("will-download", createWillDownloadHandler(deps));
  return true;
}

module.exports = {
  uniqueSavePath,
  createWillDownloadHandler,
  wireWillDownloadOnce,
};
