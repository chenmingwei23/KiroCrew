/**
 * Mapping tests: what a member envelope becomes, and what it deliberately does
 * not become.
 *
 * @module dsh_adapter/test/map
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { initMapper, mapEnvelope, mapEnvelopes } from '../lib/map.mjs'

/**
 * Build one member envelope.
 *
 * @param {number} seq - the log position.
 * @param {string} type - the event type.
 * @param {object} data - the payload.
 * @param {number} time - epoch ms.
 * @returns {object} the envelope.
 */
const envelope = (seq, type, data, time) => ({ type, seq, time, data })

test('a slot span becomes a step that opens and closes', () => {
  const state = initMapper()
  const events = mapEnvelopes(state, [
    envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000),
    envelope(1, 'slot/closed', { slot_key: 'chat-1' }, 1600),
  ])
  assert.deepEqual(events.map(e => e.type), ['step/start', 'assistant/message', 'step/end'])
  assert.equal(events[0].data.turn, 1)
  assert.equal(events[0].data.step, 1)
  assert.equal(events[0].time, 1000)
  assert.equal(events[2].time, 1600)
})

test('a message inside a span becomes a text delta', () => {
  const state = initMapper()
  mapEnvelope(state, envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000))
  const events = mapEnvelope(state, envelope(1, 'member/message', { ts: 1.2, preview: 'on it' }, 1200))
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'assistant/chunk')
  assert.deepEqual(events[0].data.chunk, { type: 'text-delta', text: 'on it' })
})

test('an empty preview maps to nothing, because the fold would drop it anyway', () => {
  const state = initMapper()
  mapEnvelope(state, envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000))
  assert.deepEqual(mapEnvelope(state, envelope(1, 'member/message', { preview: '' }, 1200)), [])
})

test('a message outside a span maps to nothing', () => {
  const state = initMapper()
  assert.deepEqual(mapEnvelope(state, envelope(0, 'member/message', { preview: 'hi' }, 1000)), [])
})

test('a close with no matching open maps to nothing, so an unseen span is not counted', () => {
  const state = initMapper()
  assert.deepEqual(mapEnvelope(state, envelope(0, 'slot/closed', { slot_key: 'chat-1' }, 1000)), [])
})

test('a close naming a different slot leaves the live span open', () => {
  const state = initMapper()
  mapEnvelope(state, envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000))
  assert.deepEqual(mapEnvelope(state, envelope(1, 'slot/closed', { slot_key: 'chat-9' }, 1100)), [])
  assert.notEqual(state.open, null)
  assert.equal(mapEnvelope(state, envelope(2, 'slot/closed', { slot_key: 'chat-1' }, 1200)).length, 2)
})

test('an overlapping open is skipped and counted, never allowed to replace the live span', () => {
  const state = initMapper()
  mapEnvelope(state, envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000))
  assert.deepEqual(mapEnvelope(state, envelope(1, 'slot/opened', { slot_key: 'chat-2' }, 1100)), [])
  assert.equal(state.overlapped, 1)
  assert.equal(state.open.slotKey, 'chat-1')
})

test('distinct slots take distinct turn numbers, in first-seen order', () => {
  const state = initMapper()
  mapEnvelopes(state, [
    envelope(0, 'slot/opened', { slot_key: 'chat-1' }, 1000),
    envelope(1, 'slot/closed', { slot_key: 'chat-1' }, 1100),
    envelope(2, 'slot/opened', { slot_key: 'chat-2' }, 1200),
    envelope(3, 'slot/closed', { slot_key: 'chat-2' }, 1300),
    envelope(4, 'slot/opened', { slot_key: 'chat-1' }, 1400),
  ])
  assert.deepEqual(state.turns, { 'chat-1': 1, 'chat-2': 2 })
})

test('unmapped member event types produce nothing', () => {
  const state = initMapper()
  for (const type of ['member/config', 'member/binding', 'member/rules', 'activity/record',
    'patrol/started', 'patrol/stopped']) {
    assert.deepEqual(mapEnvelope(state, envelope(0, type, { slot_key: 'chat-1' }, 1000)), [], type)
  }
})

test('a slot key that is missing or not a string opens nothing', () => {
  const state = initMapper()
  assert.deepEqual(mapEnvelope(state, envelope(0, 'slot/opened', {}, 1000)), [])
  assert.deepEqual(mapEnvelope(state, envelope(1, 'slot/opened', { slot_key: 7 }, 1000)), [])
  assert.equal(state.open, null)
})
