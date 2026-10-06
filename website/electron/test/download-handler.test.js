"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {
  uniqueSavePath,
  createWillDownloadHandler,
  wireWillDownloadOnce,
} = require("../download-handler");

const DOWNLOADS = "/home/u/Downloads";

// A fake DownloadItem: records the save path set on it and lets a test drive
// the one-shot "done" event with a chosen final state.
function fakeItem(filename) {
  const item = {
    _savePath: null,
    _doneHandler: null,
    _cancelled: false,
    getFilename: () => filename,
    setSavePath: (p) => { item._savePath = p; },
    cancel: () => { item._cancelled = true; },
    once: (ev, handler) => { if (ev === "done") item._doneHandler = handler; },
    emitDone: (state) => item._doneHandler && item._doneHandler({}, state),
  };
  return item;
}

const fakeApp = { getPath: (name) => (name === "downloads" ? DOWNLOADS : `/${name}`) };

describe("uniqueSavePath", () => {
  it("uses the plain path when nothing exists", () => {
    const fs = { existsSync: () => false };
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export.zip"),
    );
  });

  it("suffixes ' (1)', ' (2)' past existing files, keeping the extension", () => {
    const taken = new Set([
      path.join(DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export (1).zip"),
    ]);
    const fs = { existsSync: (p) => taken.has(p) };
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export (2).zip"),
    );
  });

  it("skips a reserved path even when it does not yet exist on disk", () => {
    // Nothing on disk, but "export.zip" is already claimed by an in-flight
    // download -> the next request must pick "export (1).zip", not reuse it.
    const fs = { existsSync: () => false };
    const reserved = new Set([path.join(DOWNLOADS, "export.zip")]);
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip", 1000, reserved),
      path.join(DOWNLOADS, "export (1).zip"),
    );
  });

  it("returns null rather than an occupied path when the budget is exhausted", () => {
    const fs = { existsSync: () => true };
    // maxTries=3: plain, (1), (2), (3) are all taken -> no free name -> null,
    // so the caller refuses the download instead of overwriting (1000)/(3).
    const out = uniqueSavePath(fs, DOWNLOADS, "export.zip", 3);
    assert.equal(out, null);
  });

  it("handles an extensionless filename", () => {
    const taken = new Set([path.join(DOWNLOADS, "data")]);
    const fs = { existsSync: (p) => taken.has(p) };
    assert.equal(uniqueSavePath(fs, DOWNLOADS, "data"), path.join(DOWNLOADS, "data (1)"));
  });
});

describe("createWillDownloadHandler", () => {
  it("sets a save path under the downloads dir so the file actually lands (issue #13047)", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs });
    const item = fakeItem("kirocrew-export.zip");
    handler({}, item);
    assert.equal(item._savePath, path.join(DOWNLOADS, "kirocrew-export.zip"));
  });

  it("logs nothing extra when the download completes (no OS file-manager reveal)", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const handler = createWillDownloadHandler({ app: fakeApp, fs, log: (...a) => logs.push(a.join(" ")) });
    const item = fakeItem("export.zip");
    handler({}, item);
    item.emitDone("completed");
    // A completed download is silent: no reveal, no log line.
    assert.equal(logs.length, 0);
  });

  it("logs when the download is interrupted", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    handler({}, item);
    item.emitDone("interrupted");
    assert.match(logs.join("\n"), /did not complete.*interrupted/);
  });

  it("cancels and logs instead of overwriting when no free filename is left (F1)", () => {
    // Every candidate under Downloads already exists -> uniqueSavePath returns
    // null -> the handler must cancel the download and never call setSavePath
    // with an occupied path (which would overwrite an existing file).
    const fs = { existsSync: () => true };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    handler({}, item);
    assert.equal(item._savePath, null);
    assert.equal(item._cancelled, true);
    assert.match(logs.join("\n"), /no free filename.*cancelling/);
  });

  it("gives two concurrent same-named downloads distinct paths, and frees the path on done (F1 concurrency)", () => {
    // Nothing on disk; two downloads named "report.pdf" start before either
    // lands. Without an in-flight reservation both would get "report.pdf" and
    // one would overwrite the other.
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs });
    const first = fakeItem("report.pdf");
    const second = fakeItem("report.pdf");
    handler({}, first);
    handler({}, second);
    assert.equal(first._savePath, path.join(DOWNLOADS, "report.pdf"));
    assert.equal(second._savePath, path.join(DOWNLOADS, "report (1).pdf"));
    assert.notEqual(first._savePath, second._savePath);

    // When the first completes, its reservation is released, so a later
    // download can reuse the (now disk-check-governed) base name again.
    first.emitDone("completed");
    const third = fakeItem("report.pdf");
    handler({}, third);
    // second is still in flight holding "report (1).pdf"; the base name is free
    // again (fs still reports nothing on disk in this fake).
    assert.equal(third._savePath, path.join(DOWNLOADS, "report.pdf"));
  });

  it("falls back without throwing when the save path cannot be computed", () => {
    const fs = { existsSync: () => false };
    const throwingApp = { getPath: () => { throw new Error("no downloads dir"); } };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: throwingApp, fs, log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    // Must not throw out of the event handler.
    assert.doesNotThrow(() => handler({}, item));
    assert.equal(item._savePath, null);
    assert.match(logs.join("\n"), /could not set save path/);
  });
});

describe("wireWillDownloadOnce", () => {
  // A fake session that records how many will-download listeners were attached.
  function fakeSession() {
    const session = {
      willDownloadListeners: 0,
      on(event) { if (event === "will-download") session.willDownloadListeners += 1; return session; },
    };
    return session;
  }

  it("attaches the handler exactly once per session, even across many windows", () => {
    const fs = { existsSync: () => false };
    const session = fakeSession();
    // Every window that shares this session calls through here.
    const first = wireWillDownloadOnce(session, { app: fakeApp, fs });
    const second = wireWillDownloadOnce(session, { app: fakeApp, fs });
    const third = wireWillDownloadOnce(session, { app: fakeApp, fs });
    assert.equal(first, true);
    assert.equal(second, false);
    assert.equal(third, false);
    assert.equal(session.willDownloadListeners, 1);
  });

  it("wires separate sessions independently", () => {
    const fs = { existsSync: () => false };
    const a = fakeSession();
    const b = fakeSession();
    assert.equal(wireWillDownloadOnce(a, { app: fakeApp, fs }), true);
    assert.equal(wireWillDownloadOnce(b, { app: fakeApp, fs }), true);
    assert.equal(a.willDownloadListeners, 1);
    assert.equal(b.willDownloadListeners, 1);
  });

  it("is a no-op for a missing session", () => {
    assert.equal(wireWillDownloadOnce(null, { app: fakeApp }), false);
  });
});
