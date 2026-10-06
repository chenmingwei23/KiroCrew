// The still-latest verdict as a PURE function.
//
// "Is the staged build still the newest thing the followed channel serves?" is
// one question with three live answers, and until now it was spread across the
// `update-available` / `update-not-available` handlers in feed-lane.js as inline
// branches that also mutated staged state and emitted renderer events. That made
// the verdict impossible to consult from anywhere that must NOT touch state --
// most of all a quiet feed read that only wants the answer -- so the only way to
// ask the question was to run the stateful `autoUpdater.checkForUpdates()`
// pipeline and suppress every side effect it produced.
//
// This factors the decision out of the side effects. `classifyFeedVerdict`
// takes the four facts the verdict is actually a function of -- the staged
// version, the version the feed just offered (or `null` for the feed's
// "nothing available" answer), the channel the feed was configured for, and the
// running build -- and returns a tagged verdict with NO I/O, NO mutation and NO
// emit. The feed-lane handlers map that verdict onto their state transitions and
// events; a side-effect-free feed read can call the same function and get the
// same answer without any of them.
//
// The direction gate itself (`shouldAutoOffer`, the "running ahead of the
// channel's published latest" downgrade-nag suppressor) already lived as a pure
// helper in auto-update.js and is PASSED IN here rather than duplicated, so the
// one copy of that rule stays the one copy.

/**
 * @typedef {(
 *   | "offer"        // candidate passes the direction gate and no stage is held:
 *                    //   a fresh, offerable update.
 *   | "suppress"     // direction gate says no (same channel, not newer): the
 *                    //   difference-based feed is nagging a downgrade. Any stage
 *                    //   armed for it must be discarded; report up to date.
 *   | "still-latest" // a stage is held and the feed still offers exactly it:
 *                    //   re-surface the install, keep the stage.
 *   | "superseded"   // a stage is held but the feed offers a DIFFERENT offerable
 *                    //   version: drop the stale stage and re-find the newer one.
 *   | "retracted"    // the feed offers nothing newer (candidate === null) while a
 *                    //   stage is held: the stage was withdrawn or the channel was
 *                    //   switched back. Discard the stage.
 *   | "up-to-date"   // the feed offers nothing newer and no stage is held.
 * )} StillLatestVerdict
 */

/**
 * Classify what a feed response means for the still-latest question, as a pure
 * function of the four facts the verdict depends on. No side effects: the caller
 * owns every state change and emit the verdict implies.
 *
 * The `candidate === null` case is the feed's "update-not-available" answer
 * (electron-updater fires that, not `update-available`, when the followed lane
 * publishes exactly the running version). It is the retraction / channel-switch
 * -back signal when a stage is held, and plain up-to-date otherwise.
 *
 * When a candidate IS offered, the direction gate runs FIRST: a same-channel
 * version that is not newer than the running build is the difference-based feed
 * reporting a downgrade (allowDowngrade=true), which must be suppressed before
 * the stage comparison -- a stage armed for such a candidate is itself
 * suppressed, not "still latest". Only a candidate that clears the gate reaches
 * the stage comparison, where it is `still-latest` iff it equals the held stage.
 *
 * @param {{
 *   stagedVersion: (string|null),
 *   candidate: (string|null),
 *   followedChannel: (string|null),
 *   runningVersion: string,
 *   defaultChannel: (string|null),
 *   shouldAutoOffer: (o: {candidate:string, current:string, followedChannel:(string|null), defaultChannel:(string|null)}) => boolean,
 * }} o
 * @returns {StillLatestVerdict}
 */
function classifyFeedVerdict({
  stagedVersion,
  candidate,
  followedChannel,
  runningVersion,
  defaultChannel,
  shouldAutoOffer,
}) {
  const hasStage = !!stagedVersion;

  // The feed's "nothing newer" answer. With a stage held this is the retraction
  // / channel-switch-back path (a feed repointed to the running version would
  // otherwise leave a withdrawn or wrong-channel build staged to install on
  // quit); with no stage it is plain up-to-date.
  if (candidate === null || candidate === undefined) {
    return hasStage ? "retracted" : "up-to-date";
  }

  // Direction gate, before the stage comparison. A same-channel candidate that
  // is not newer than the running build is a downgrade the difference-based
  // feed reported; suppress it (and discard any stage armed for it). A
  // deliberate channel switch and any newer version pass through.
  const offerable = shouldAutoOffer({
    candidate,
    current: runningVersion,
    followedChannel,
    defaultChannel,
  });
  if (!offerable) {
    return "suppress";
  }

  // Offerable candidate. If a stage is held, the running version never changes
  // mid-session so the feed reports the staged version as "available" too --
  // equality is what separates "the stage is still the latest" (re-surface)
  // from "a newer build superseded the stage" (drop and re-find).
  if (hasStage) {
    return candidate === stagedVersion ? "still-latest" : "superseded";
  }

  // No stage held: a fresh offerable update.
  return "offer";
}

module.exports = { classifyFeedVerdict };
