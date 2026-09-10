/**
 * Wire tests: the adapter's client against the fake gateway's REAL WebSocket
 * server, using Node's own WebSocket.
 *
 * The rest of the driver suite injects a socket, which is what makes dropping a
 * frame possible. That leaves one thing unproven: whether the client works over
 * an actual connection at all — the handshake, the framing, and §3's rule that
 * `eventlog_subscribed` arrives before any event frame. This file covers exactly
 * that, so the pod run is not the first time real bytes move.
 *
 * @module dsh_adapter/test/wire
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { FakeGateway } from '../fake/gateway.mjs'
import { ContractClient } from '../lib/contract.mjs'
import { decodeFrames, encodeTextFrame } from '../fake/wsserver.mjs'

const CONTRIBUTIONS = { events: ['dsh-adapter/*'], projections: ['dsh-adapter/*'], units: ['member'] }

/**
 * Wait for a condition.
 *
 * @param {() => boolean} predicate - the condition.
 * @param {string} what - what is awaited, for the failure message.
 * @param {number} [timeoutMs] - how long to wait.
 * @returns {Promise<void>} resolves once the condition holds.
 */
async function waitFor(predicate, what, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error(`timed out waiting for ${what}`)
}

test('a frame survives a round trip through the encoder and decoder', () => {
  // A masked client frame is what the server actually receives.
  const payload = Buffer.from(JSON.stringify({ type: 'eventlog_subscribe' }), 'utf8')
  const mask = Buffer.from([1, 2, 3, 4])
  const masked = Buffer.from(payload)
  for (let i = 0; i < masked.length; i += 1) masked[i] ^= mask[i % 4]
  const frame = Buffer.concat([Buffer.from([0x81, 0x80 | payload.length]), mask, masked])
  const { frames, rest } = decodeFrames(frame)
  assert.equal(frames.length, 1)
  assert.equal(frames[0].text, payload.toString('utf8'))
  assert.equal(rest.length, 0)

  // A long payload takes the 16-bit length path.
  const long = 'x'.repeat(300)
  const encoded = encodeTextFrame(long)
  assert.equal(encoded[1] & 0x7f, 126)
  assert.equal(encoded.length, 4 + 300)
})

test('a partial frame is left for the next chunk rather than mis-decoded', () => {
  const whole = encodeTextFrame('hello')
  const { frames, rest } = decodeFrames(whole.subarray(0, 3))
  assert.equal(frames.length, 0)
  assert.equal(rest.length, 3)
})

test('subscribed arrives before any event frame, over a real socket', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  const client = new ContractClient({ baseUrl, token: gateway.token })
  gateway.append('member', 'kiro', 'slot/opened', { slot_key: 'chat-1' }, 1000)

  const order = []
  const streamed = []
  const handle = client.subscribe({
    kind: 'member',
    id: 'kiro',
    onSubscribed: lastSeq => order.push(`subscribed:${lastSeq}`),
    onEvent: event => {
      order.push(`event:${event.seq}`)
      streamed.push(event)
    },
  })
  try {
    await waitFor(() => order.length > 0, 'the subscribed frame')
    gateway.append('member', 'kiro', 'slot/closed', { slot_key: 'chat-1' }, 1600)
    await waitFor(() => streamed.length === 1, 'the streamed append')
    assert.deepEqual(order, ['subscribed:0', 'event:1'])
    assert.equal(streamed[0].type, 'slot/closed')
    assert.equal(streamed[0].seq, 1)
  } finally {
    handle.close()
    await gateway.close()
  }
})

test('a socket with the wrong token is closed rather than subscribed', async () => {
  const gateway = new FakeGateway({ contributions: CONTRIBUTIONS })
  const baseUrl = await gateway.listen()
  const client = new ContractClient({ baseUrl, token: 'not-the-token' })
  let closedReason = ''
  const handle = client.subscribe({
    kind: 'member',
    id: 'kiro',
    onSubscribed: () => {},
    onEvent: () => {},
    onClosed: reason => {
      closedReason = reason
    },
  })
  try {
    await waitFor(() => closedReason !== '', 'the socket to be closed')
    assert.ok(closedReason === 'closed' || closedReason === 'error', closedReason)
  } finally {
    handle.close()
    await gateway.close()
  }
})
