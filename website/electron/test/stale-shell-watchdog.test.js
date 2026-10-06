"use strict";
//
// Blank-dashboard watchdog: on a dashboard `did-finish-load` with no
// `dashboard:booted` ping within the window, force one cache-ignoring reload,
// bounded so a build that can never boot does not loop. Pure logic with
// injected timer/clock, exercised without a live BrowserWindow.
//

const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_BOOT_TIMEOUT_MS,
  isDashboardDocument,
  createStaleShellWatchdog,
} = require("../stale-shell-watchdog");

const BACKEND = "http://127.0.0.1:5476";

// A controllable timer: setTimer records the pending callback; `fire()` runs it.
function fakeTimers() {
  let pending = null;
  let nextId = 1;
  return {
    setTimer(fn) {
      const id = nextId++;
      pending = { id, fn };
      return id;
    },
    clearTimer(id) {
      if (pending && pending.id === id) pending = null;
    },
    fire() {
      const p = pending;
      pending = null;
      if (!p) throw new Error("no timer armed");
      return p.fn();
    },
    get armed() {
      return pending !== null;
    },
  };
}

function make(overrides = {}) {
  const timers = fakeTimers();
  const logs = [];
  let reloads = 0;
  const gaveUp = [];
  let clock = 1_000_000;
  const wd = createStaleShellWatchdog({
    backendUrl: BACKEND,
    reloadIgnoringCache: () => { reloads += 1; },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    log: (m) => logs.push(m),
    onGiveUp: (info) => gaveUp.push(info),
    now: () => clock,
    ...overrides,
  });
  return { wd, timers, logs, gaveUp, reloads: () => reloads, advance: (ms) => { clock += ms; } };
}

test("isDashboardDocument: exact origin, not prefix", () => {
  assert.equal(isDashboardDocument("http://127.0.0.1:5476/chat", BACKEND), true);
  assert.equal(isDashboardDocument("http://127.0.0.1:54760/", BACKEND), false, "port prefix must not match");
  assert.equal(isDashboardDocument("http://127.0.0.1:7779/", BACKEND), false, "a remote-crew pane origin");
  assert.equal(isDashboardDocument("file:///splash/loading.html", BACKEND), false, "the boot splash");
  assert.equal(isDashboardDocument("not a url", BACKEND), false);
});

test("arms only for the dashboard origin", () => {
  const { wd } = make();
  wd.noteDocumentLoaded("file:///splash/loading.html");
  assert.equal(wd.armed, false, "splash must not arm the watch");
  wd.noteDocumentLoaded("http://127.0.0.1:7779/");
  assert.equal(wd.armed, false, "a remote pane must not arm the watch");
  wd.noteDocumentLoaded(`${BACKEND}/chat?token=x`);
  assert.equal(wd.armed, true, "the dashboard document arms it");
});

test("a healthy boot ping disarms the watch and no reload happens", () => {
  const { wd, timers, reloads } = make();
  wd.noteDocumentLoaded(`${BACKEND}/`);
  assert.equal(wd.armed, true);
  wd.noteBooted();
  assert.equal(wd.armed, false, "the boot ping cancels the pending timer");
  assert.equal(timers.armed, false);
  assert.equal(reloads(), 0);
});

test("loaded but never booted → one cache-ignoring reload", () => {
  const { wd, timers, reloads, logs } = make();
  wd.noteDocumentLoaded(`${BACKEND}/`);
  const verdict = timers.fire();
  assert.equal(verdict, "reloaded");
  assert.equal(reloads(), 1);
  assert.ok(logs.some((l) => l.includes("no boot within") && l.includes("attempt 1/2")));
});

test("bounded: gives up after the attempt cap within the window", () => {
  const { wd, timers, reloads, gaveUp } = make({ maxAttempts: 2, windowMs: 10 * 60_000 });
  // Each failed load arms, then times out.
  wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(timers.fire(), "reloaded");
  wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(timers.fire(), "reloaded");
  wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(timers.fire(), "gave-up");
  assert.equal(reloads(), 2, "no third reload past the cap");
  assert.equal(gaveUp.length, 1);
});

test("the sliding window frees the budget once the span passes", () => {
  const h = make({ maxAttempts: 1, windowMs: 60_000 });
  h.wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(h.timers.fire(), "reloaded");
  h.wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(h.timers.fire(), "gave-up");
  h.advance(60_001); // the first reload ages out of the window
  h.wd.noteDocumentLoaded(`${BACKEND}/`); assert.equal(h.timers.fire(), "reloaded");
  assert.equal(h.reloads(), 2);
});

test("ignored during quit — no reload while tearing down", () => {
  const { wd, timers, reloads } = make({ isQuitting: () => true });
  wd.noteDocumentLoaded(`${BACKEND}/`);
  assert.equal(timers.fire(), "ignored-quitting");
  assert.equal(reloads(), 0);
});

test("a re-load replaces the pending timer (one window per load)", () => {
  const { wd, timers } = make();
  wd.noteDocumentLoaded(`${BACKEND}/`);
  const first = timers.armed;
  wd.noteDocumentLoaded(`${BACKEND}/chat`); // e.g. the reload this watch triggered
  assert.equal(first, true);
  assert.equal(wd.armed, true, "still exactly one timer armed after re-load");
});

test("a reloadIgnoringCache that throws is swallowed (never escapes the emitter)", () => {
  const { wd, timers } = make({ reloadIgnoringCache: () => { throw new Error("destroyed"); } });
  wd.noteDocumentLoaded(`${BACKEND}/`);
  assert.equal(timers.fire(), "reloaded"); // decision still returns; throw is caught
});

test("DEFAULT_BOOT_TIMEOUT_MS is a sane, generous default", () => {
  assert.ok(DEFAULT_BOOT_TIMEOUT_MS >= 10_000);
});
