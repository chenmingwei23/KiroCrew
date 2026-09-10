/**
 * The adapter process.
 *
 * Boots the foreign plugin runtime once, then drives one unit per member: read
 * the log, fold it through the plugin, publish the view. Serves a health
 * endpoint on `PORT` so the app platform can see it is up, and reports what it
 * is hosting there rather than only in the log.
 *
 * Configuration, all from the environment:
 *
 * | variable                    | meaning                                              |
 * | --------------------------- | ---------------------------------------------------- |
 * | `PORT`                      | health server port (the platform assigns it)         |
 * | `DSH_ADAPTER_CHECKOUT`      | absolute path to the read-only plugin checkout       |
 * | `DSH_ADAPTER_GATEWAY`       | gateway origin; defaults to loopback on `KIROCREW_PORT` |
 * | `DSH_ADAPTER_TOKEN`         | the app bearer token (§2)                            |
 * | `DSH_ADAPTER_UNITS`         | comma-separated unit ids; omit to discover members   |
 *
 * Without a token the process still starts and still serves health, reporting
 * `contributing: false` and why. That is deliberate: the platform hands an app
 * backend no outbound credential today, so a hard exit would turn a known
 * platform gap into a crash-looping app.
 *
 * @module dsh_adapter/server
 */

import { createServer } from 'node:http'
import process from 'node:process'
import { startHost } from './lib/host.mjs'
import { ContractClient } from './lib/contract.mjs'
import { UnitDriver } from './lib/unit.mjs'

/** The app's own name; §2 requires every declared pattern to start with it. */
export const APP_NAME = 'dsh-adapter'

/** Checkout-relative entry of the hosted plugin. */
export const PLUGIN_ENTRY = 'packages/session/session-stats/src/index.ts'

/** The unit kind this adapter contributes to. */
export const UNIT_KIND = 'member'

/**
 * Render schema per plugin key (§7).
 *
 * Every label says what the number means FOR A MEMBER and names the plugin's
 * own field, because the mapping gives those fields a member's meaning: a
 * "step" is a slot span, not a model step. Naming both keeps the card readable
 * without misreporting what was folded. The three fields a member's log cannot
 * feed are left off the card rather than shown as a permanent zero.
 */
export const RENDER_SCHEMAS = Object.freeze({
  sessionStats: {
    title: 'Session stats',
    kind: 'keyvalue',
    fields: [
      { title: 'Slots driven (turns)', path: 'turns' },
      { title: 'Slot spans (steps)', path: 'steps' },
      { title: 'Slot wall time ms (llmMs)', path: 'llmMs' },
      { title: 'Time to first message ms (ttftMs)', path: 'ttftMs' },
      { title: 'Spans with a message (ttftSteps)', path: 'ttftSteps' },
    ],
  },
})

/**
 * Structured line logger on stdout, which the platform captures to the app log.
 *
 * @param {string} message - what happened.
 * @param {object} [detail] - fields worth keeping.
 * @returns {void}
 */
function log(message, detail = {}) {
  const line = { ts: new Date().toISOString(), app: APP_NAME, message, ...detail }
  process.stdout.write(`${JSON.stringify(line)}\n`)
}

/**
 * Read configuration from the environment.
 *
 * @param {Record<string, string | undefined>} [env] - the environment; defaults to the process env.
 * @returns {{port: number, checkout: string, gateway: string, token: string, units: string[]}} the configuration.
 */
export function readConfig(env = process.env) {
  const gatewayPort = env.DSH_ADAPTER_GATEWAY ? '' : (env.KIROCREW_PORT ?? '5476')
  return {
    port: Number(env.PORT ?? '0'),
    checkout: env.DSH_ADAPTER_CHECKOUT ?? '',
    gateway: env.DSH_ADAPTER_GATEWAY ?? `http://127.0.0.1:${gatewayPort}`,
    token: env.DSH_ADAPTER_TOKEN ?? '',
    units: (env.DSH_ADAPTER_UNITS ?? '').split(',').map(part => part.trim()).filter(Boolean),
  }
}

/**
 * The unit ids to drive.
 *
 * Configuration wins. Absent it, the dashboard's own member list is asked —
 * §3 addresses a unit by id and offers no way to enumerate the units a
 * contributor may subscribe to, so there is nothing in the protocol to ask.
 *
 * @param {ContractClient} client - the protocol client, for its base URL and token.
 * @param {string[]} configured - explicitly configured unit ids.
 * @returns {Promise<string[]>} the unit ids.
 */
export async function resolveUnits(client, configured) {
  if (configured.length > 0) return configured
  try {
    const response = await fetch(`${client.baseUrl}/api/members`, { headers: client.headers() })
    if (!response.ok) {
      log('member discovery refused', { status: response.status })
      return []
    }
    const body = await response.json()
    const rows = Array.isArray(body) ? body : (body?.members ?? [])
    return rows.map(row => row?.slug).filter(slug => typeof slug === 'string' && slug !== '')
  } catch (error) {
    log('member discovery failed', { error: String(error) })
    return []
  }
}

/**
 * Boot the adapter.
 *
 * @param {object} [options] - boot options.
 * @param {Record<string, string | undefined>} [options.env] - the environment.
 * @returns {Promise<{state: object, stop: () => Promise<void>}>} the running adapter.
 */
export async function main({ env = process.env } = {}) {
  const config = readConfig(env)
  const state = {
    app: APP_NAME,
    contributing: false,
    reason: '',
    plugin: null,
    keys: [],
    units: {},
  }

  const server = createServer((request, response) => {
    const path = (request.url ?? '').split('?')[0]
    if (path !== '/health') {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ code: 'not_found', error: path }))
      return
    }
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ ok: true, ...state, units: unitReport(state) }))
  })
  await new Promise(resolve => server.listen(config.port, '127.0.0.1', resolve))
  log('health server listening', { port: server.address().port })

  const drivers = []
  const stop = async () => {
    for (const driver of drivers) driver.stop()
    await new Promise(resolve => server.close(resolve))
  }

  if (!config.checkout) {
    state.reason = 'DSH_ADAPTER_CHECKOUT is not set, so there is no plugin to host'
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }

  let host = null
  try {
    host = await startHost({ checkoutRoot: config.checkout, pluginEntry: PLUGIN_ENTRY })
  } catch (error) {
    state.reason = `plugin host failed to boot: ${String(error)}`
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }
  state.plugin = host.plugin
  state.keys = host.keys.map(key => `${APP_NAME}/${key}`)
  log('plugin mounted', { plugin: host.plugin, keys: host.keys, stateVersions: host.stateVersions })

  if (!config.token) {
    state.reason = 'no app token: the platform passes an app backend no outbound credential'
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }

  const client = new ContractClient({ baseUrl: config.gateway, token: config.token })
  const units = await resolveUnits(client, config.units)
  if (units.length === 0) {
    state.reason = 'no units to drive'
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }

  for (const id of units) {
    const driver = new UnitDriver({
      client, host, kind: UNIT_KIND, id, keyPrefix: APP_NAME, log, schemas: RENDER_SCHEMAS,
    })
    drivers.push(driver)
    state.units[id] = driver.stats
    try {
      await driver.start()
    } catch (error) {
      log('driver failed to start', { id, error: String(error) })
    }
  }
  state.contributing = true
  log('contributing', { units: units.length, keys: state.keys })
  return { state, stop }
}

/**
 * The per-unit stats block for the health payload.
 *
 * @param {object} state - the adapter state.
 * @returns {Record<string, object>} a plain copy of each unit's stats.
 */
function unitReport(state) {
  return Object.fromEntries(Object.entries(state.units).map(([id, stats]) => [id, { ...stats }]))
}

// Only boot when run as the entry point, so the tests can import the module.
if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  const running = await main()
  const shutdown = () => void running.stop().then(() => process.exit(0))
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}
