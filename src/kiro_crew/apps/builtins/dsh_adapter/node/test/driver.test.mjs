/**
 * Driver tests against the fake gateway: catch-up, live streaming, the §3
 * contiguity rule, and the §5 publish rules.
 *
 * These run the real HTTP surface on loopback and the real client code. The §3
 * event frames arrive through the fake's injected socket, which is also the only
 * way to drop one on purpose — and dropping one is the whole point, because a
 * gap the adapter folds across is a view that stays wrong with no error
 * anywhere.
 *
 * @module dsh_adapter/test/driver
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { FakeGateway } from '../fake/gateway.mjs'
import { ContractClient } from '../lib/contract.mjs'
import { UnitDriver } from '../lib/unit.mjs'
import { startHost } from '../lib/host.mjs'
import { checkoutPath } from '../lib/loader.mjs'
import { APP_NAME, PLUGIN_ENTRY, RENDER_SCHEMAS, UNIT_KIND } from '../server.mjs'

const CHECKOUT = process.env.DSH_ADAPTER_CHECKOUT ?? '/local/home/mingweic/oss/deepseek-harness'
const skip = existsSync(checkoutPath(CHECKOUT, PLUGIN_ENTRY)) ? false : 'no plugin checkout'
const KEY = `${APP_NAME}/sessionStats`
const CONTRIBUTIONS = { events: [`${APP_NAME}/*`], projections: [`${APP_NAME}/*`], units: [UNIT_KIND] }

/**
 * Poll until a predicate holds.
 *
 * The driver coalesces publishes behind a timer, so a test that asserts
 * immediately after an append is asserting on the wrong moment.
 *
 * @param {() => boolean} predicate - the condition to wait for.
 * @param {string} what - what is being waited for, for the failure message.
 * @param {number} [timeoutMs] - how long to wait.
 * @returns {Promise<void>} resolves once the predicate holds.
 */
async function waitFor(predicate, what, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`timed out waiting for ${what}`)
}

/**
 * Stand up a gateway, a host, a client, and a driver for one member.
 *
 * @param {object} [options] - harness options.
 * @param {string} [options.id] - the member slug.
 * @param {object} [options.contributions] - the app's declared contributions.
 * @returns {Promise<object>} the harness plus a `stop`.
 */
async function harness({ id = 'kiro', contributions = CONTRIBUTIONS } = {}) {
  const gateway = new FakeGateway({ contributions })
  const baseUrl = await gateway.listen()
  const host = await startHost({ checkoutRoot: CHECKOUT, pluginEntry: PLUGIN_ENTRY })
  const lines = []
  const client = new ContractClient({ baseUrl, token: gateway.token, socketImpl: gateway.socketClass() })
  const driver = new UnitDriver({
    client,
    host,
    kind: UNIT_KIND,
    id,
    keyPrefix: APP_NAME,
    log: (message, detail) => lines.push({ message, ...detail }),
    schemas: RENDER_SCHEMAS,
  })
  return {
    gateway,
    host,
    client,
    driver,
    id,
    lines,
    row: () => gateway.row(UNIT_KIND, id, KEY),
    async stop() {
      driver.stop()
      await gateway.close()
      await host.dispose()
    },
  }
}

/**
 * Write one closed slot span into a log.
 *
 * @param {FakeGateway} gateway - the gateway.
 * @param {string} id - the member slug.
 * @param {string} slotKey - the slot key.
 * @param {number} start - epoch ms the slot opened.
 * @param {number} end - epoch ms the slot closed.
 * @returns {void}
 */
function span(gateway, id, slotKey, start, end) {
  gateway.append(UNIT_KIND, id, 'slot/opened', { slot_key: slotKey }, start)
  gateway.append(UNIT_KIND, id, 'member/message', { preview: 'working' }, start + 100)
  gateway.append(UNIT_KIND, id, 'slot/closed', { slot_key: slotKey, reason: 'closed' }, end)
}

test('catch-up folds existing history and publishes it', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row() !== undefined, 'the first publish')
    const row = h.row()
    assert.equal(row.seq, 2, 'current as of the last folded log position')
    assert.equal(row.stateVersion, 1)
    assert.equal(row.value.steps, 1)
    assert.equal(row.value.llmMs, 600)
  } finally {
    await h.stop()
  }
})

test('a live append folds on top and republishes at the higher seq', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row()?.value.steps === 1, 'the catch-up publish')

    span(h.gateway, h.id, 'chat-2', 2000, 2400)
    await waitFor(() => h.row()?.value.steps === 2, 'the streamed publish')
    assert.equal(h.row().value.turns, 2)
    assert.equal(h.row().value.llmMs, 600 + 400)
    // Published as of the last folded log position, whatever that number is:
    // the adapter's own §4 marker also occupies a seq in this log.
    assert.equal(h.row().seq, h.driver.lastSeq)
    assert.equal(h.row().seq, h.gateway.log(UNIT_KIND, h.id).length - 1)
    assert.equal(h.driver.stats.refolds, 0, 'a contiguous stream needs no refold')
  } finally {
    await h.stop()
  }
})

test('a dropped frame is detected as a gap and repaired by refolding the log', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row()?.value.steps === 1, 'the catch-up publish')

    // The gateway may close a slow subscriber, so a frame can simply not arrive.
    h.gateway.dropNextEvents = 1
    span(h.gateway, h.id, 'chat-2', 2000, 2400)

    await waitFor(() => h.driver.stats.refolds === 1, 'the gap repair')
    await waitFor(() => h.row()?.value.steps === 2, 'the publish after refolding')

    // The repaired value must equal a fold that never saw a gap at all.
    const clean = h.host.createFold()
    const { events } = await h.client.readAll(UNIT_KIND, h.id, -1)
    clean.push(events)
    assert.deepEqual(h.row().value, clean.values().sessionStats)
    assert.equal(h.row().seq, h.gateway.log(UNIT_KIND, h.id).length - 1)
    assert.ok(h.lines.some(line => line.message === 'gap detected, refolding'))
  } finally {
    await h.stop()
  }
})

test('the fold is never advanced across a gap', { skip }, async () => {
  const h = await harness()
  try {
    await h.driver.start()
    // Hand the driver a run that skips seq 1 outright.
    await h.driver.ingest([
      { type: 'slot/opened', seq: 0, time: 1000, data: { slot_key: 'chat-1' } },
      { type: 'slot/closed', seq: 2, time: 1600, data: { slot_key: 'chat-1' } },
    ])
    assert.equal(h.driver.stats.refolds, 1)
    // Refold read the real log, which is empty, so nothing is folded.
    assert.equal(h.driver.lastSeq, -1)
    assert.equal(h.driver.fold.values().sessionStats.steps, 0)
  } finally {
    await h.stop()
  }
})

test('a replayed envelope is dropped rather than folded twice', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row()?.seq === 2, 'the catch-up publish')
    const { events } = await h.client.readAll(UNIT_KIND, h.id, -1)
    await h.driver.ingest(events)
    assert.equal(h.driver.stats.refolds, 0)
    assert.equal(h.driver.fold.values().sessionStats.steps, 1)
  } finally {
    await h.stop()
  }
})

test('republishing a position the gateway already stored is refused and counted', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row()?.value.steps === 1, 'the first publish')

    // Stop the stream and pin the driver to the position the row already holds,
    // which is what a replay or a slower contributor looks like from §5's side.
    // A publish at a HIGHER position is not a replay and is rightly accepted, so
    // the position has to be stated rather than inferred from a race with the
    // adapter's own §4 marker echoing back through its subscription.
    h.driver.subscription?.close()
    h.driver.subscription = null
    const stored = h.row().seq
    h.driver.lastSeq = stored
    await h.driver.publish()
    assert.equal(h.driver.stats.stale, 1)
    assert.equal(h.row().seq, stored, 'the stored row did not move')

    // And one below it is refused the same way.
    h.driver.lastSeq = stored - 1
    await h.driver.publish()
    assert.equal(h.driver.stats.stale, 2)
    assert.equal(h.row().seq, stored)
  } finally {
    await h.stop()
  }
})

test('a higher stateVersion replaces the row regardless of seq', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row()?.value.steps === 1, 'the first publish')

    const result = await h.client.publishProjection(UNIT_KIND, h.id, KEY, { steps: 99 }, 0, 2)
    assert.equal(result.published, true)
    assert.equal(h.row().stateVersion, 2)
    assert.equal(h.row().seq, 0)
  } finally {
    await h.stop()
  }
})

test('the render schema and one attach marker are written once, not per publish', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.row() !== undefined, 'the first publish')
    await waitFor(
      () => h.gateway.schemas.get(`${UNIT_KIND}/${h.id}/${KEY}`) !== undefined,
      'the schema publish',
    )
    assert.equal(h.gateway.schemas.get(`${UNIT_KIND}/${h.id}/${KEY}`).kind, 'keyvalue')

    await waitFor(
      () => h.gateway.log(UNIT_KIND, h.id).some(e => e.type === `${APP_NAME}/projection-attached`),
      'the attach marker',
    )
    span(h.gateway, h.id, 'chat-2', 2000, 2400)
    await waitFor(() => h.driver.stats.published >= 2, 'a second publish')
    const markers = h.gateway.log(UNIT_KIND, h.id).filter(e => e.type === `${APP_NAME}/projection-attached`)
    assert.equal(markers.length, 1)
    assert.equal(markers[0].data.stateVersion, 1)
    // The gateway assigns seq and time; the contributor never supplies them.
    assert.equal(typeof markers[0].seq, 'number')
    assert.equal(typeof markers[0].time, 'number')
  } finally {
    await h.stop()
  }
})

test('a restart does not add a second attach marker for the same fold version', { skip }, async () => {
  const h = await harness()
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(
      () => h.gateway.log(UNIT_KIND, h.id).some(e => e.type === `${APP_NAME}/projection-attached`),
      'the attach marker',
    )
    h.driver.stop()

    // A second driver over the same log is what a gateway restart looks like.
    const restarted = new UnitDriver({
      client: h.client,
      host: h.host,
      kind: UNIT_KIND,
      id: h.id,
      keyPrefix: APP_NAME,
      log: () => {},
      schemas: RENDER_SCHEMAS,
    })
    try {
      await restarted.start()
      await waitFor(() => restarted.stats.published + restarted.stats.stale > 0, 'the restart publish')
      const markers = h.gateway.log(UNIT_KIND, h.id).filter(e => e.type === `${APP_NAME}/projection-attached`)
      assert.equal(markers.length, 1)
    } finally {
      restarted.stop()
    }
  } finally {
    await h.stop()
  }
})

test('a key outside the declared patterns stops the driver instead of retrying', { skip }, async () => {
  const h = await harness({ contributions: { ...CONTRIBUTIONS, projections: ['other-app/*'] } })
  try {
    span(h.gateway, h.id, 'chat-1', 1000, 1600)
    await h.driver.start()
    await waitFor(() => h.driver.stopped, 'the driver to stand down')
    assert.equal(h.row(), undefined)
    assert.ok(h.lines.some(line => line.message === 'publish failed' && line.code === 'projection_key_not_owned'))
  } finally {
    await h.stop()
  }
})

test('a built-in key cannot be published from outside', { skip }, async () => {
  const h = await harness({ contributions: { ...CONTRIBUTIONS, projections: ['roster', `${APP_NAME}/*`] } })
  try {
    const result = await h.client.publishProjection(UNIT_KIND, h.id, 'roster', { x: 1 }, 0, 1)
      .then(() => 'published', error => error.code)
    assert.equal(result, 'projection_key_not_owned')
  } finally {
    await h.stop()
  }
})

test('an event type outside the declared patterns is refused', { skip }, async () => {
  const h = await harness()
  try {
    const code = await h.client.appendEvent(UNIT_KIND, h.id, 'member/message', { preview: 'forged' })
      .then(() => 'appended', error => error.code)
    assert.equal(code, 'event_type_not_owned')
  } finally {
    await h.stop()
  }
})

test('an ungranted unit kind is refused', { skip }, async () => {
  const h = await harness({ contributions: { ...CONTRIBUTIONS, units: [] } })
  try {
    const code = await h.client.readEvents(UNIT_KIND, h.id, -1)
      .then(() => 'read', error => error.code)
    assert.equal(code, 'unit_kind_not_granted')
  } finally {
    await h.stop()
  }
})

test('catch-up pages through a log longer than one page', { skip }, async () => {
  const h = await harness()
  try {
    for (let i = 0; i < 210; i += 1) {
      span(h.gateway, h.id, `chat-${i}`, 10_000 + i * 10, 10_005 + i * 10)
    }
    const { events, lastSeq } = await h.client.readAll(UNIT_KIND, h.id, -1)
    assert.equal(events.length, 630)
    assert.equal(lastSeq, 629)
    assert.deepEqual(events.map(e => e.seq).slice(0, 3), [0, 1, 2])
  } finally {
    await h.stop()
  }
})
