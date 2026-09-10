/**
 * Fold tests: the adapter's hosted fold against the same plugin run in a plain
 * harness.
 *
 * The oracle matters more than the numbers. The adapter drives the plugin
 * through the runtime's real projection registry, which caches per-session
 * state, validates through the unit's own schema, and back-fills cells it has
 * not seen. The harness here does none of that: it takes the plugin's exported
 * definition and calls `init`, `apply`, `view` in a loop. If the two disagree,
 * the adapter's plumbing changed the plugin's answer, which is the one thing it
 * must never do.
 *
 * Skipped, with a reason, when the plugin checkout is not present.
 *
 * @module dsh_adapter/test/fold
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { startHost } from '../lib/host.mjs'
import { installLoader, checkoutPath } from '../lib/loader.mjs'
import { initMapper, mapEnvelopes } from '../lib/map.mjs'
import { PLUGIN_ENTRY } from '../server.mjs'

const CHECKOUT = process.env.DSH_ADAPTER_CHECKOUT ?? '/local/home/mingweic/oss/deepseek-harness'
const available = existsSync(checkoutPath(CHECKOUT, PLUGIN_ENTRY))

/** A member log with two closed spans, a message in each, and noise between. */
const LOG = [
  { type: 'member/config', seq: 0, time: 900, data: { kiro_agent: 'kirocrew', changed: ['model'] } },
  { type: 'slot/opened', seq: 1, time: 1000, data: { slot_key: 'chat-1' } },
  { type: 'member/message', seq: 2, time: 1200, data: { ts: 1.2, preview: 'looking' } },
  { type: 'member/message', seq: 3, time: 1300, data: { ts: 1.3, preview: 'still looking' } },
  { type: 'slot/closed', seq: 4, time: 1600, data: { slot_key: 'chat-1', reason: 'closed' } },
  { type: 'activity/record', seq: 5, time: 1700, data: { ts: '2026-09-10T00:00:00Z', member: 'Kiro' } },
  { type: 'patrol/started', seq: 6, time: 1800, data: { slot_key: 'chat-2' } },
  { type: 'slot/opened', seq: 7, time: 2000, data: { slot_key: 'chat-2' } },
  { type: 'member/message', seq: 8, time: 2100, data: { ts: 2.1, preview: 'done' } },
  { type: 'slot/closed', seq: 9, time: 2500, data: { slot_key: 'chat-2', reason: 'closed' } },
]

/**
 * Fold the plugin's own definition by hand, with no registry involved.
 *
 * @param {Array<object>} pluginEvents - the mapped plugin events, in order.
 * @returns {Promise<object>} the plugin's view of that stream.
 */
async function harnessFold(pluginEvents) {
  installLoader({ checkoutRoot: CHECKOUT })
  const projectionPath = checkoutPath(CHECKOUT, 'packages/session/session-stats/src/projection.ts')
  const { sessionStatsProjectionDefinition: unit } = await import(`file://${projectionPath}`)
  let state = unit.init()
  for (const event of pluginEvents) state = unit.apply(state, event)
  return unit.view(state)
}

test('the hosted fold equals the plugin folded in a plain harness', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    const fold = host.createFold()
    fold.push(LOG)
    const hosted = fold.values().sessionStats

    const expected = await harnessFold(mapEnvelopes(initMapper(), LOG))
    assert.deepEqual(hosted, expected)

    // The figures a member's log can honestly feed.
    assert.equal(hosted.turns, 2, 'two distinct slots driven')
    assert.equal(hosted.steps, 2, 'two closed spans')
    assert.equal(hosted.llmMs, 600 + 500, 'summed slot wall time')
    assert.equal(hosted.ttftMs, 200 + 100, 'open to first message, per span')
    assert.equal(hosted.ttftSteps, 2)
    // The figures it cannot: no tool pairs and no token usage exist in a member log.
    assert.equal(hosted.toolMs, 0)
    assert.equal(hosted.decodeMs, 0)
    assert.equal(hosted.decodeTokens, 0)
  } finally {
    await host.dispose()
  }
})

test('folding in pieces equals folding in one push', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    const whole = host.createFold()
    whole.push(LOG)

    const pieces = host.createFold()
    for (const envelope of LOG) pieces.push([envelope])

    assert.deepEqual(pieces.values(), whole.values())
  } finally {
    await host.dispose()
  }
})

test('the plugin mounts active and registers exactly its own key', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    assert.equal(host.plugin.name, 'session-stats')
    assert.deepEqual(host.plugin.inject, ['sessionProjections'])
    assert.deepEqual(host.keys, ['sessionStats'])
    assert.equal(host.stateVersions.sessionStats, 1)
  } finally {
    await host.dispose()
  }
})

test('an empty log yields the plugin initial view, not an absent key', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    const values = host.createFold().values()
    assert.deepEqual(values.sessionStats, await harnessFold([]))
  } finally {
    await host.dispose()
  }
})

test('two folds on one host do not share state', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    const first = host.createFold()
    first.push(LOG)
    const second = host.createFold()
    assert.equal(second.values().sessionStats.steps, 0)
    assert.equal(first.values().sessionStats.steps, 2)
  } finally {
    await host.dispose()
  }
})

test('an overlapping span is reported rather than silently dropped', { skip: available ? false : 'no plugin checkout' }, async () => {
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  try {
    const fold = host.createFold()
    fold.push([
      { type: 'slot/opened', seq: 0, time: 1000, data: { slot_key: 'chat-1' } },
      { type: 'slot/opened', seq: 1, time: 1100, data: { slot_key: 'chat-2' } },
      { type: 'slot/closed', seq: 2, time: 1500, data: { slot_key: 'chat-1' } },
    ])
    assert.equal(fold.overlapped(), 1)
    assert.equal(fold.values().sessionStats.steps, 1)
    assert.equal(fold.values().sessionStats.llmMs, 500)
  } finally {
    await host.dispose()
  }
})
