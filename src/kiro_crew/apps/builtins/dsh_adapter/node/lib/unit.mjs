/**
 * Drives one unit: read its log, fold it through the hosted plugin, publish the
 * result, then keep both in step as events stream in.
 *
 * The §3 event channel is a delta channel, so this file's central obligation is
 * the contiguity rule: an event whose `seq` is not exactly one past what has
 * been folded means events were missed, and folding it anyway would produce a
 * view that is quietly wrong forever. The response is always the same — throw
 * the fold away, start a new one, and re-read the log from the beginning. That
 * is why {@link Host.createFold} exists rather than a reset method: a dropped
 * fold takes the mapper's span tracking with it, and a half-reset would leave a
 * span open that no close will ever match.
 *
 * Three moments can deliver an out-of-order event and all three take that same
 * path: a gap in the live stream, a reconnect after the gateway closed a slow
 * subscriber, and the window between sending `eventlog_subscribe` and the
 * catch-up read returning. The last one is why streamed events are buffered
 * while catch-up is in flight instead of being folded as they arrive.
 *
 * @module dsh_adapter/unit
 */

import { ContractError } from './contract.mjs'

/** How long to coalesce publishes, so a burst of appends is one POST. */
const PUBLISH_DEBOUNCE_MS = 250

/** How long to wait before reconnecting a closed subscription. */
const RECONNECT_DELAY_MS = 1000

/**
 * @typedef {object} UnitStats
 * @property {number} folded - envelopes folded since this driver started.
 * @property {number} refolds - times the fold was dropped and rebuilt (gaps and reconnects).
 * @property {number} published - projection publishes the gateway accepted.
 * @property {number} stale - publishes refused as `stale_seq`.
 * @property {number} lastSeq - the log position the published view is current as of.
 */

/**
 * Drive one unit end to end.
 */
export class UnitDriver {
  /**
   * @param {object} options - driver options.
   * @param {import('./contract.mjs').ContractClient} options.client - the protocol client.
   * @param {import('./host.mjs').Host} options.host - the booted plugin host.
   * @param {string} options.kind - the unit kind, e.g. `member`.
   * @param {string} options.id - the unit id.
   * @param {string} options.keyPrefix - the app's own projection namespace, e.g. `dsh-adapter`.
   * @param {(message: string, detail?: object) => void} options.log - structured logger.
   * @param {Record<string, object>} [options.schemas] - §7 render schema per plugin key.
   */
  constructor({ client, host, kind, id, keyPrefix, log, schemas = {} }) {
    this.client = client
    this.host = host
    this.kind = kind
    this.id = id
    this.keyPrefix = keyPrefix
    this.log = log
    this.schemas = schemas

    this.fold = host.createFold()
    this.lastSeq = -1
    this.buffered = []
    this.catchingUp = false
    this.subscription = null
    this.stopped = false
    this.publishTimer = null
    this.registered = false
    /** @type {Set<string>} `key@stateVersion` markers already in the log. */
    this.attached = new Set()
    this.stats = { folded: 0, refolds: 0, published: 0, stale: 0, lastSeq: -1 }
  }

  /**
   * Note what an envelope means to the driver itself, on its way to the fold.
   *
   * Only the adapter's own attach markers matter here: they say the log already
   * records this key at this fold version, so it must not be recorded again.
   *
   * @param {object} envelope - one member log envelope.
   * @returns {void}
   */
  note(envelope) {
    if (envelope?.type !== `${this.keyPrefix}/projection-attached`) return
    const data = envelope.data ?? {}
    this.attached.add(`${String(data.key)}@${String(data.stateVersion)}`)
  }

  /**
   * The published key for one of the plugin's projection keys.
   *
   * §2 requires every published key to start with the app's own name followed
   * by `/`, so the plugin's own key is namespaced rather than published raw —
   * which also keeps it clear of the built-in keys the gateway owns.
   *
   * @param {string} pluginKey - the key the plugin registered.
   * @returns {string} the key to publish under.
   */
  publishedKey(pluginKey) {
    return `${this.keyPrefix}/${pluginKey}`
  }

  /**
   * Start driving: subscribe, catch up, publish, and stay current.
   *
   * @returns {Promise<void>} resolves once the first catch-up and publish are done.
   */
  async start() {
    this.openSubscription()
    await this.catchUp()
  }

  /** Stop driving and release the subscription. */
  stop() {
    this.stopped = true
    if (this.publishTimer !== null) clearTimeout(this.publishTimer)
    this.publishTimer = null
    this.subscription?.close()
    this.subscription = null
  }

  /**
   * Open (or reopen) the §3 subscription.
   *
   * Streamed events are buffered while a catch-up read is in flight: they are
   * newer than the read's result but arrive before it, so folding them
   * immediately would fold out of order.
   */
  openSubscription() {
    if (this.stopped) return
    this.subscription = this.client.subscribe({
      kind: this.kind,
      id: this.id,
      onSubscribed: lastSeq => {
        this.log('subscribed', { kind: this.kind, id: this.id, lastSeq })
      },
      onEvent: event => {
        if (this.catchingUp) this.buffered.push(event)
        else void this.ingest([event])
      },
      onClosed: reason => {
        this.subscription = null
        if (this.stopped) return
        this.log('subscription closed', { kind: this.kind, id: this.id, reason })
        // A closed subscriber resumes by catch-up from its last folded seq and
        // re-subscribing (§3) -- no fold is dropped for a clean close, because
        // the catch-up read proves what was missed.
        setTimeout(() => {
          if (this.stopped) return
          this.openSubscription()
          void this.catchUp()
        }, RECONNECT_DELAY_MS)
      },
    })
  }

  /**
   * Read everything past the folded position and fold it.
   *
   * @returns {Promise<void>} resolves once the read, the fold, and the publish are done.
   */
  async catchUp() {
    if (this.stopped) return
    this.catchingUp = true
    try {
      const { events } = await this.client.readAll(this.kind, this.id, this.lastSeq)
      await this.ingest(events, { fromCatchUp: true })
    } catch (error) {
      this.log('catch-up failed', { kind: this.kind, id: this.id, error: String(error) })
    } finally {
      this.catchingUp = false
    }
    const queued = this.buffered
    this.buffered = []
    if (queued.length > 0) await this.ingest(queued)
  }

  /**
   * Fold a run of envelopes, enforcing the contiguity rule.
   *
   * An envelope at or below the folded position is a replay and is dropped. An
   * envelope above `lastSeq + 1` is a gap: the fold is discarded and the log is
   * re-read from the start. Never folds across a gap.
   *
   * @param {Array<object>} envelopes - envelopes in seq order.
   * @param {object} [options] - ingest options.
   * @param {boolean} [options.fromCatchUp] - true when the run came from a catch-up read.
   * @returns {Promise<void>} resolves once the run is folded and a publish is scheduled.
   */
  async ingest(envelopes, { fromCatchUp = false } = {}) {
    if (this.stopped || envelopes.length === 0) return
    const run = []
    for (const envelope of envelopes) {
      const seq = envelope?.seq
      if (typeof seq !== 'number') continue
      if (seq <= this.lastSeq) continue
      if (seq !== this.lastSeq + 1) {
        // A gap. Re-reading from the start is the only sound repair, and it is
        // safe to do from a catch-up read too: the read is authoritative, so a
        // gap inside its own result means the log itself moved under us.
        this.log('gap detected, refolding', {
          kind: this.kind, id: this.id, expected: this.lastSeq + 1, got: seq, fromCatchUp,
        })
        await this.refold()
        return
      }
      run.push(envelope)
      this.note(envelope)
      this.lastSeq = seq
    }
    if (run.length === 0) return
    this.fold.push(run)
    this.stats.folded += run.length
    this.stats.lastSeq = this.lastSeq
    this.schedulePublish()
  }

  /**
   * Drop the fold and rebuild it from the whole log.
   *
   * @returns {Promise<void>} resolves once the log has been re-read and folded.
   */
  async refold() {
    this.fold = this.host.createFold()
    this.lastSeq = -1
    this.stats.refolds += 1
    this.buffered = []
    this.attached.clear()
    this.catchingUp = true
    try {
      const { events } = await this.client.readAll(this.kind, this.id, -1)
      // Straight fold: the read starts at the beginning, so contiguity is the
      // read's own invariant and re-entering ingest would risk a refold loop.
      const contiguous = []
      let expected = 0
      for (const envelope of events) {
        if (envelope?.seq !== expected) break
        contiguous.push(envelope)
        this.note(envelope)
        expected += 1
      }
      this.fold.push(contiguous)
      this.lastSeq = contiguous.length - 1
      this.stats.folded += contiguous.length
      this.stats.lastSeq = this.lastSeq
      if (contiguous.length !== events.length) {
        this.log('log has a gap inside a full read', {
          kind: this.kind, id: this.id, read: events.length, folded: contiguous.length,
        })
      }
    } catch (error) {
      this.log('refold failed', { kind: this.kind, id: this.id, error: String(error) })
    } finally {
      this.catchingUp = false
    }
    this.schedulePublish()
  }

  /** Coalesce a burst of appends into one publish. */
  schedulePublish() {
    if (this.stopped || this.publishTimer !== null) return
    this.publishTimer = setTimeout(() => {
      this.publishTimer = null
      void this.publish()
    }, PUBLISH_DEBOUNCE_MS)
  }

  /**
   * Publish the current value of every key the plugin registered (§5).
   *
   * @returns {Promise<void>} resolves once every key has been offered to the gateway.
   */
  async publish() {
    if (this.stopped) return
    const values = this.fold.values()
    for (const pluginKey of this.host.keys) {
      if (!Object.hasOwn(values, pluginKey)) continue
      const key = this.publishedKey(pluginKey)
      const stateVersion = this.host.stateVersions[pluginKey] ?? 0
      try {
        const result = await this.client.publishProjection(
          this.kind, this.id, key, values[pluginKey], this.lastSeq, stateVersion,
        )
        if (result.published) this.stats.published += 1
        else this.stats.stale += 1
        await this.register(key, pluginKey, stateVersion)
      } catch (error) {
        const code = error instanceof ContractError ? error.code : ''
        this.log('publish failed', { kind: this.kind, id: this.id, key, code, error: String(error) })
        // A key this app may not publish will never succeed; stop driving rather
        // than retrying a refusal on every append.
        if (code === 'projection_key_not_owned') this.stop()
      }
    }
  }

  /**
   * Register the view: its render schema (§7) and one durable marker (§4).
   *
   * The schema is published once per process, because it is not stored in the
   * log and a gateway that lost it has no other way to get it back.
   *
   * The marker is published once per `(key, fold version)` FOR THE LIFE OF THE
   * LOG, not once per boot. The log is never rewritten (§6), so a marker per
   * boot would grow it forever while saying the same thing; the fact worth
   * keeping is that this key started being served by this fold version, and
   * that happens once. The catch-up read has already folded the whole log by
   * the time this runs, so an existing marker is known.
   *
   * @param {string} key - the published key.
   * @param {string} pluginKey - the plugin's own key.
   * @param {number} stateVersion - the fold version now serving the key.
   * @returns {Promise<void>} resolves once both have been attempted.
   */
  async register(key, pluginKey, stateVersion) {
    if (!this.registered) {
      this.registered = true
      const schema = this.schemas[pluginKey]
      if (schema) {
        try {
          const result = await this.client.publishSchema(this.kind, this.id, key, schema)
          if (!result.published) this.log('render schema not accepted', { key, status: result.status })
        } catch (error) {
          this.log('render schema publish failed', { key, error: String(error) })
        }
      }
    }
    const marker = `${key}@${stateVersion}`
    if (this.attached.has(marker)) return
    // Recorded before the append so a failure cannot loop, and so the append
    // that echoes back through this driver's own subscription is a no-op.
    this.attached.add(marker)
    try {
      await this.client.appendEvent(this.kind, this.id, `${this.keyPrefix}/projection-attached`, {
        key, plugin: this.host.plugin.name, stateVersion,
      })
    } catch (error) {
      const code = error instanceof ContractError ? error.code : ''
      this.log('attach marker not appended', { key, code, error: String(error) })
    }
  }
}
