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
 * | `KIROCREW_APP_NAME`         | the app name the gateway knows (the platform sets it) |
 * | `KIROCREW_PROXY_SECRET`     | the app's own secret (the platform sets it)          |
 * | `DSH_ADAPTER_CHECKOUT`      | absolute path to the read-only plugin checkout       |
 * | `DSH_ADAPTER_GATEWAY`       | gateway origin                                        |
 * | `DSH_ADAPTER_TOKEN`         | a token to use instead of exchanging the secret      |
 * | `DSH_ADAPTER_UNITS`         | comma-separated unit ids; omit to discover members   |
 *
 * With anything missing the process still starts and still serves health,
 * reporting `contributing: false` and why. A hard exit would turn a
 * configuration gap into a crash-looping app.
 *
 * @module dsh_adapter/server
 */

import { createServer } from 'node:http'
import process from 'node:process'
import { startHost } from './lib/host.mjs'
import { ContractClient, ContractError, exchangeToken } from './lib/contract.mjs'
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
 * The shape is the host's, not ours: `kind` picks one of five body shapes and
 * `path` is a list of dotted selectors, which for `keyvalue` names the fields to
 * show AND supplies their labels. So a label cannot carry a gloss — it is the
 * selector. The five fields listed are the ones a member's log can honestly
 * feed; `toolMs`, `decodeMs` and `decodeTokens` are left off rather than shown
 * as a permanent zero, and the title names the plugin so its own field names
 * read as its own.
 */
export const RENDER_SCHEMAS = Object.freeze({
  sessionStats: {
    kind: 'keyvalue',
    title: 'session-stats (hosted plugin)',
    path: ['turns', 'steps', 'llmMs', 'ttftMs', 'ttftSteps'],
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
 * `secret` comes from the platform itself: a backend is handed its own app
 * secret as `KIROCREW_PROXY_SECRET`, which is the same value the token exchange
 * validates — so an app can authenticate outbound with nothing configured. A
 * `DSH_ADAPTER_TOKEN` is still honoured for a run against a standalone contract
 * server that mints no tokens.
 *
 * @param {Record<string, string | undefined>} [env] - the environment; defaults to the process env.
 * @returns {{port: number, checkout: string, gateway: string, token: string, secret: string,
 *   appName: string, units: string[]}} the configuration.
 */
export function readConfig(env = process.env) {
  return {
    port: Number(env.PORT ?? '0'),
    checkout: env.DSH_ADAPTER_CHECKOUT ?? '',
    gateway: env.DSH_ADAPTER_GATEWAY ?? '',
    token: env.DSH_ADAPTER_TOKEN ?? '',
    secret: env.KIROCREW_PROXY_SECRET ?? '',
    appName: env.KIROCREW_APP_NAME ?? APP_NAME,
    units: (env.DSH_ADAPTER_UNITS ?? '').split(',').map(part => part.trim()).filter(Boolean),
  }
}

/**
 * The app-scoped token to call the gateway with.
 *
 * A configured token wins, so a standalone contract server needs no exchange.
 * Otherwise the platform-supplied secret is exchanged for one, RETRYING while
 * the gateway refuses to connect: at boot the platform starts an app backend
 * before its own HTTP listener is up, so a single attempt loses that race and
 * the app would sit there not contributing until someone restarted it. A refusal
 * that is not a connection failure -- a wrong secret -- is final and returns at
 * once rather than being retried for a minute.
 *
 * @param {{gateway: string, token: string, secret: string, appName: string}} config - the configuration.
 * @param {object} [options] - retry options.
 * @param {number} [options.attempts] - how many times to try connecting.
 * @param {number} [options.delayMs] - wait between attempts.
 * @returns {Promise<{token: string, reason: string}>} the token, or the reason there is none.
 */
export async function resolveToken(config, { attempts = 20, delayMs = 3000 } = {}) {
  if (config.token) return { token: config.token, reason: '' }
  if (!config.secret) {
    return { token: '', reason: 'no app secret in the environment and no DSH_ADAPTER_TOKEN configured' }
  }
  let last = ''
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const token = await exchangeToken({
        baseUrl: config.gateway, appName: config.appName, secret: config.secret,
      })
      return { token, reason: '' }
    } catch (error) {
      last = String(error)
      // A ContractError carries a status, so the gateway answered and the
      // refusal is about the secret, not the connection.
      if (error instanceof ContractError) return { token: '', reason: `app token exchange refused: ${last}` }
      if (attempt < attempts) {
        log('gateway not reachable yet, retrying token exchange', { attempt, of: attempts })
        await new Promise(resolve => setTimeout(resolve, delayMs))
      }
    }
  }
  return { token: '', reason: `app token exchange failed after ${attempts} attempts: ${last}` }
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
    const response = await fetch(client.url('/api/members'), { headers: client.headers() })
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

  if (!config.gateway) {
    state.reason = 'DSH_ADAPTER_GATEWAY is not set, so there is no gateway to contribute to'
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }
  const { token, reason } = await resolveToken(config)
  if (!token) {
    state.reason = reason
    log('not contributing', { reason: state.reason })
    return { state, stop }
  }

  const client = new ContractClient({ baseUrl: config.gateway, token })
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
