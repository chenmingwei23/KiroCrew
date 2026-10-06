const { test } = require("node:test");
const assert = require("node:assert");

const { classifyFeedVerdict } = require("../runtime/update/still-latest");
// The real direction gate, so the verdict is tested against the SAME helper the
// live feed lane wires in -- not a stand-in that could drift from it.
const { shouldAutoOffer } = require("../auto-update");

// classifyFeedVerdict is the still-latest verdict extracted from the
// update-available / update-not-available handlers into a pure function. These
// tests pin each verdict against the four facts it is a function of, so a quiet
// feed read and the live handlers provably get the same answer.

const STABLE = "stable";

function verdict(over = {}) {
  return classifyFeedVerdict({
    stagedVersion: null,
    candidate: null,
    followedChannel: STABLE,
    runningVersion: "1.0.0",
    defaultChannel: STABLE,
    shouldAutoOffer,
    ...over,
  });
}

// --- the feed's "nothing newer" answer (candidate === null) ---------------

test("no candidate, no stage -> up-to-date", () => {
  assert.strictEqual(verdict({ candidate: null, stagedVersion: null }), "up-to-date");
});

test("no candidate while a stage is held -> retracted", () => {
  assert.strictEqual(
    verdict({ candidate: null, stagedVersion: "1.2.0" }),
    "retracted",
  );
});

test("undefined candidate is treated as the no-candidate answer", () => {
  assert.strictEqual(
    verdict({ candidate: undefined, stagedVersion: "1.2.0" }),
    "retracted",
  );
});

// --- direction gate runs before the stage comparison ----------------------

test("same-channel candidate not newer than running -> suppress", () => {
  // Running 2.0.0, feed offers 1.0.0 on the same channel: a downgrade the
  // difference-based feed reported. Suppressed.
  assert.strictEqual(
    verdict({ candidate: "1.0.0", runningVersion: "2.0.0" }),
    "suppress",
  );
});

test("suppress wins even when the not-newer candidate equals the stage", () => {
  // A stage armed for a now-not-newer candidate must be suppressed, NOT read as
  // "still latest" -- the gate runs first.
  assert.strictEqual(
    verdict({ candidate: "1.0.0", runningVersion: "2.0.0", stagedVersion: "1.0.0" }),
    "suppress",
  );
});

test("deliberate channel switch exempts the direction gate", () => {
  // followedChannel !== defaultChannel: the user moved off their default lane,
  // so even an older version is offerable (allowDowngrade intent).
  assert.strictEqual(
    verdict({
      candidate: "1.0.0",
      runningVersion: "2.0.0-insider.3",
      followedChannel: STABLE,
      defaultChannel: "insider",
    }),
    "offer",
  );
});

// --- offerable candidate, with/without a stage ----------------------------

test("newer candidate, no stage -> offer", () => {
  assert.strictEqual(
    verdict({ candidate: "2.0.0", runningVersion: "1.0.0", stagedVersion: null }),
    "offer",
  );
});

test("offerable candidate equal to the held stage -> still-latest", () => {
  assert.strictEqual(
    verdict({ candidate: "2.0.0", runningVersion: "1.0.0", stagedVersion: "2.0.0" }),
    "still-latest",
  );
});

test("offerable candidate newer than the held stage -> superseded", () => {
  assert.strictEqual(
    verdict({ candidate: "2.1.0", runningVersion: "1.0.0", stagedVersion: "2.0.0" }),
    "superseded",
  );
});

// --- purity: unrankable comparison fails OPEN via shouldAutoOffer ----------

test("unrankable candidate is offerable (fail-open), matching the gate", () => {
  // isNewerVersion -> null for a garbage version; shouldAutoOffer returns true,
  // so the verdict offers rather than silently hiding it.
  assert.strictEqual(
    verdict({ candidate: "not-a-version", runningVersion: "1.0.0", stagedVersion: null }),
    "offer",
  );
});
