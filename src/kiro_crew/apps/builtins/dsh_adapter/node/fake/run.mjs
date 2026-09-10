/**
 * Runs the fake gateway as a standalone process, so the adapter can be exercised
 * end to end while the real routes do not exist yet.
 *
 * Seeds one member log with two closed slot spans, then appends another span
 * every few seconds, which is what makes the §3 stream and the republish visible
 * rather than inferred.
 *
 *     node fake/run.mjs --port 7999 --token dev-token --unit kiro
 *
 * Test-support code. Never loaded by the adapter itself.
 *
 * @module dsh_adapter/fake/run
 */

import process from 'node:process'
import { FakeGateway } from './gateway.mjs'

/**
 * Read `--name value` arguments.
 *
 * @param {string[]} argv - the argument list.
 * @returns {Record<string, string>} the parsed flags.
 */
function flags(argv) {
  const out = {}
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i].startsWith('--')) out[argv[i].slice(2)] = argv[i + 1] ?? ''
  }
  return out
}

const args = flags(process.argv.slice(2))
const token = args.token || 'dev-token'
const unit = args.unit || 'kiro'
const port = Number(args.port || '0')
const appName = args.app || 'dsh-adapter'

const gateway = new FakeGateway({
  token,
  contributions: { events: [`${appName}/*`], projections: [`${appName}/*`], units: ['member'] },
})
// A fixed port is what the adapter's config points at, so bind it explicitly
// rather than taking the ephemeral one `listen()` chooses.
gateway.server = null
await gateway.listen()
if (port !== 0 && gateway.server.address().port !== port) {
  await new Promise(resolve => gateway.server.close(resolve))
  await new Promise(resolve => gateway.server.listen(port, '127.0.0.1', resolve))
  gateway.baseUrl = `http://127.0.0.1:${port}`
}
process.stdout.write(`fake gateway on ${gateway.baseUrl} for unit member/${unit}\n`)

/**
 * Write one closed slot span into the seeded member's log.
 *
 * @param {string} slotKey - the slot key.
 * @param {number} start - epoch ms the slot opened.
 * @param {number} heldMs - how long it was held.
 * @returns {void}
 */
function span(slotKey, start, heldMs) {
  gateway.append('member', unit, 'slot/opened', { slot_key: slotKey }, start)
  gateway.append('member', unit, 'member/message', { preview: `working in ${slotKey}` }, start + 120)
  gateway.append('member', unit, 'slot/closed', { slot_key: slotKey, reason: 'closed' }, start + heldMs)
}

const base = Date.now() - 600_000
gateway.append('member', unit, 'member/config', { kiro_agent: 'kirocrew', changed: ['model'] }, base)
span('chat-1', base + 1_000, 4_200)
span('chat-2', base + 60_000, 15_500)

let next = 3
setInterval(() => {
  span(`chat-${next}`, Date.now(), 2_000 + next * 500)
  next += 1
  const row = gateway.row('member', unit, `${appName}/sessionStats`)
  process.stdout.write(`${new Date().toISOString()} row=${JSON.stringify(row)}\n`)
}, 15_000).unref?.()

process.on('SIGTERM', () => void gateway.close().then(() => process.exit(0)))
process.on('SIGINT', () => void gateway.close().then(() => process.exit(0)))
// Keep the process alive for the interval above.
setInterval(() => {}, 1 << 30)
