/**
 * Boots the foreign plugin runtime and mounts the hosted plugin unmodified.
 *
 * Everything here is the runtime's own code, loaded from the read-only
 * checkout: its DI kernel, its projection registry service, and the plugin.
 * The adapter supplies no substitute for any of them, so the fold that runs is
 * the plugin's, driven by the registry that normally drives it. What the
 * adapter does supply is the event stream (see `./map.mjs`) and the synthetic
 * session object the registry keys its per-unit state by.
 *
 * One detail of the registry decides the shape of this file. It caches per
 * `(unit, session)` state in a `WeakMap` and, when an event arrives for a
 * session it has no cell for, back-fills by slicing the session's log at the
 * event's own `seq`. Mapped events carry no meaningful `seq` — one envelope can
 * produce two of them — so that path must never be taken. Reading a snapshot
 * immediately after a session is created builds the cell from an empty log up
 * front, and every later event then finds it. {@link createFold} does that, so
 * no caller has to know.
 *
 * Resetting a fold is therefore just a new session object: the old cell is
 * unreachable and collected, with no registry API for eviction needed.
 *
 * @module dsh_adapter/host
 */

import { installLoader, checkoutPath } from './loader.mjs'
import { initMapper, mapEnvelopes } from './map.mjs'

/**
 * @typedef {object} HostedPlugin
 * @property {string} name - the plugin's declared Cordis name.
 * @property {string[]} inject - the services it declares as required.
 * @property {string} entry - absolute path to the plugin source that was loaded.
 */

/**
 * @typedef {object} Host
 * @property {HostedPlugin} plugin - what was mounted.
 * @property {string[]} keys - projection keys the mounted plugin registered.
 * @property {Record<string, number>} stateVersions - each key's fold version, read from the registry.
 * @property {() => Fold} createFold - start a fresh fold over one log.
 * @property {() => Promise<void>} dispose - unload the plugin and tear the runtime down.
 */

/**
 * @typedef {object} Fold
 * @property {(envelopes: Array<object>) => void} push - fold a run of member envelopes in seq order.
 * @property {() => Record<string, unknown>} values - the current whole value per projection key.
 * @property {() => number} overlapped - `slot/opened` events the mapping had to skip.
 */

/**
 * Wait for Cordis fibers to settle.
 *
 * Mounting is asynchronous: a fiber goes PENDING until its injected services
 * exist, and the registry service installs on its own tick. A microtask flush
 * plus one macrotask is what the runtime's own tests use to observe a settled
 * tree, and it is enough here because nothing in this assembly does I/O at
 * load time.
 *
 * @returns {Promise<void>} resolves once pending fiber transitions have run.
 */
function settle() {
  return new Promise(resolve => setTimeout(resolve, 0))
}

/**
 * Boot the runtime, install its projection registry, and mount the plugin.
 *
 * @param {object} options - host options.
 * @param {string} options.checkoutRoot - absolute path to the read-only plugin checkout.
 * @param {string} options.pluginEntry - checkout-relative path to the plugin's source entry.
 * @param {string} [options.registryEntry] - checkout-relative path to the projection registry package.
 * @returns {Promise<Host>} the booted host.
 */
export async function startHost({
  checkoutRoot,
  pluginEntry,
  registryEntry = 'packages/session/session-projection/src/index.ts',
}) {
  installLoader({ checkoutRoot })

  const cordis = await import('@deepseek-ai/cordis')
  const registryModule = await import(pathToImportUrl(checkoutPath(checkoutRoot, registryEntry)))
  const pluginPath = checkoutPath(checkoutRoot, pluginEntry)
  const pluginModule = await import(pathToImportUrl(pluginPath))

  const ctx = new cordis.Context()
  ctx.plugin(registryModule.default ?? registryModule.SessionProjectionRegistry)
  await settle()
  if (!ctx.sessionProjections) {
    throw new Error('projection registry did not install as ctx.sessionProjections')
  }

  const fiber = ctx.plugin(pluginModule)
  await settle()
  const state = fiber?.state
  // FiberState.ACTIVE is 2. A fiber still PENDING (0) means a declared service
  // is missing, which is a shim gap, not a plugin fault -- say which.
  if (state !== 2) {
    const declared = Array.isArray(pluginModule.inject) ? pluginModule.inject.join(', ') : '(none declared)'
    throw new Error(
      `plugin ${String(pluginModule.name)} did not reach ACTIVE (fiber state ${String(state)}); `
      + `it declares inject [${declared}] and the host provides [sessionProjections]`,
    )
  }

  const registry = ctx.sessionProjections
  const probe = newSession()
  const checkpoint = registry.checkpoint(probe)
  const keys = Object.keys(checkpoint)
  if (keys.length === 0) throw new Error('plugin mounted but registered no projection unit')
  const stateVersions = Object.fromEntries(keys.map(key => [key, checkpoint[key].ver]))

  return {
    plugin: {
      name: String(pluginModule.name ?? 'unnamed'),
      inject: Array.isArray(pluginModule.inject) ? [...pluginModule.inject] : [],
      entry: pluginPath,
    },
    keys,
    stateVersions,
    createFold: () => createFold(ctx, registry),
    dispose: async () => {
      await fiber?.dispose?.()
      await ctx.fiber?.dispose?.()
    },
  }
}

/**
 * A synthetic session: the two fields the registry and the fold read.
 *
 * @returns {{seq: number, events: Array<object>}} an empty session.
 */
function newSession() {
  return { seq: 0, events: [] }
}

/**
 * Convert an absolute path to a URL string `import()` accepts.
 *
 * @param {string} absolute - an absolute filesystem path.
 * @returns {string} the file URL.
 */
function pathToImportUrl(absolute) {
  return new URL(`file://${absolute}`).href
}

/**
 * Start one fold over one log.
 *
 * @param {object} ctx - the runtime root context.
 * @param {object} registry - the projection registry service.
 * @returns {Fold} the fold handle.
 */
function createFold(ctx, registry) {
  const session = newSession()
  const mapper = initMapper()
  // Builds the per-unit cell from the empty log, so no later event takes the
  // registry's seq-slicing back-fill path.
  registry.snapshot(session)

  return {
    push(envelopes) {
      for (const event of mapEnvelopes(mapper, envelopes)) {
        // The registry reads seq off the session, not off the event, once the
        // cell exists; keeping the log and its length in step is what makes
        // that true.
        session.events.push(event)
        session.seq = session.events.length
        ctx.emit('session/event', session, event)
      }
    },
    values: () => registry.snapshot(session).values,
    overlapped: () => mapper.overlapped,
  }
}
