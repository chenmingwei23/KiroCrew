/**
 * A fake gateway that speaks the contribution protocol, so the adapter can be
 * developed and tested before the real routes exist.
 *
 * It enforces the parts of the contract the adapter must not violate, and
 * refuses with the same `code` values (§9) the real gateway will:
 *
 * - the gateway assigns `seq` and `time`; a contributor supplying either is
 *   ignored (§4);
 * - an event `type` or projection `key` outside the app's declared patterns is
 *   refused `event_type_not_owned` / `projection_key_not_owned` (§2);
 * - a built-in projection key cannot be published from outside (§2);
 * - one row per `(kind, id, key)`; an equal or lower `seq` is `409 stale_seq`
 *   unless `stateVersion` is higher (§5);
 * - `eventlog_subscribed` is delivered before any `eventlog_event` (§3).
 *
 * The HTTP surface is a real server on loopback. The §3 event stream is served
 * over an injected socket rather than a real WebSocket: Node ships a WebSocket
 * client but no server, and hand-rolling RFC 6455 framing here would test
 * undici's parser rather than the adapter's frame handling. {@link FakeGateway.socketClass}
 * delivers the same frames, in the same order, through the same client code
 * path — including the ability to drop frames, which is how the contiguity
 * rule gets tested at all.
 *
 * Test-support code. Never loaded by the adapter itself.
 *
 * @module dsh_adapter/fake/gateway
 */

import { createServer } from 'node:http'
import { attachWebSocketServer } from './wsserver.mjs'

/** Keys the gateway owns; §2 forbids publishing these from outside. */
const BUILTIN_KEYS = new Set(['roster', 'activity', 'wake', 'driving'])

/** Serialized `data` ceiling from §4. */
const MAX_DATA_BYTES = 64 * 1024

/**
 * Apply one client frame to a subscriber, for both the wire and injected paths.
 *
 * §3's ordering rule lives here: `eventlog_subscribed` is emitted from inside
 * the subscribe handling, before the subscription can receive any event frame.
 *
 * @param {FakeGateway} gateway - the gateway holding the logs.
 * @param {{subscriptions: Set<string>}} subscriber - the subscriber's state.
 * @param {string} text - the serialized client frame.
 * @param {(frame: object) => void} emit - sends one frame back to the client.
 * @returns {void}
 */
function applyClientFrame(gateway, subscriber, text, emit) {
  let frame = null
  try {
    frame = JSON.parse(text)
  } catch {
    return
  }
  const data = frame?.data ?? {}
  const unit = `${data.kind}/${data.id}`
  if (frame.type === 'eventlog_subscribe') {
    subscriber.subscriptions.add(unit)
    const events = gateway.log(data.kind, data.id)
    emit({
      type: 'eventlog_subscribed',
      data: { kind: data.kind, id: data.id, lastSeq: events.length - 1 },
    })
  } else if (frame.type === 'eventlog_unsubscribe') {
    subscriber.subscriptions.delete(unit)
  }
}

/**
 * Whether a value matches one of a set of `prefix/*` patterns.
 *
 * @param {string} value - the event type or projection key.
 * @param {string[]} patterns - declared patterns, e.g. `['dsh-adapter/*']`.
 * @returns {boolean} true when one pattern matches.
 */
function matches(value, patterns) {
  return patterns.some(pattern => (
    pattern.endsWith('/*') ? value.startsWith(pattern.slice(0, -1)) : value === pattern
  ))
}

/**
 * An in-memory gateway implementing §3 through §7 for one app.
 */
export class FakeGateway {
  /**
   * @param {object} [options] - gateway options.
   * @param {string} [options.token] - the bearer token it accepts.
   * @param {object} [options.contributions] - the app's declared `contributions` block.
   */
  constructor({ token = 'test-token', contributions = {} } = {}) {
    this.token = token
    this.contributions = {
      events: contributions.events ?? [],
      projections: contributions.projections ?? [],
      units: contributions.units ?? [],
    }
    /** @type {Map<string, Array<object>>} unit key to its append-only log. */
    this.logs = new Map()
    /** @type {Map<string, {value: unknown, seq: number, stateVersion: number}>} row per unit+key. */
    this.rows = new Map()
    /** @type {Map<string, object>} render schema per unit+key. */
    this.schemas = new Map()
    /** @type {Set<FakeSocket>} live sockets. */
    this.sockets = new Set()
    this.server = null
    this.baseUrl = ''
    /** Frames to drop before delivery, which simulates the gateway closing a slow subscriber. */
    this.dropNextEvents = 0
  }

  /**
   * Start the HTTP server on loopback.
   *
   * @returns {Promise<string>} the base URL.
   */
  async listen() {
    this.server = createServer((request, response) => {
      this.handle(request, response).catch(error => {
        response.writeHead(500, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ code: 'internal', error: String(error) }))
      })
    })
    attachWebSocketServer(this.server, connection => this.acceptWire(connection))
    await new Promise(resolve => this.server.listen(0, '127.0.0.1', resolve))
    this.baseUrl = `http://127.0.0.1:${this.server.address().port}`
    return this.baseUrl
  }

  /**
   * Accept one real WebSocket connection as a subscriber.
   *
   * Shares {@link applyClientFrame} with the injected socket, so both paths obey
   * the same §3 ordering rule instead of two implementations drifting apart.
   *
   * @param {object} connection - the connection from `attachWebSocketServer`.
   * @returns {void}
   */
  acceptWire(connection) {
    const query = new URL(connection.url, 'http://127.0.0.1').searchParams
    if (query.get('token') !== this.token) {
      connection.close()
      return
    }
    const subscriber = {
      readyState: 1,
      subscriptions: new Set(),
      deliver: (kind, id, event) => {
        if (!subscriber.subscriptions.has(`${kind}/${id}`)) return
        connection.send(JSON.stringify({ type: 'eventlog_event', data: { kind, id, event } }))
      },
      close: () => {
        subscriber.readyState = 3
        this.sockets.delete(subscriber)
        connection.close()
      },
    }
    this.sockets.add(subscriber)
    connection.onMessage(text => {
      applyClientFrame(this, subscriber, text, frame => connection.send(JSON.stringify(frame)))
    })
    connection.onClose(() => {
      subscriber.readyState = 3
      this.sockets.delete(subscriber)
    })
  }

  /** Stop the server and close every socket. */
  async close() {
    for (const socket of [...this.sockets]) socket.close()
    if (this.server) await new Promise(resolve => this.server.close(resolve))
    this.server = null
  }

  /**
   * The log for one unit, created on first use.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @returns {Array<object>} the unit's events.
   */
  log(kind, id) {
    const key = `${kind}/${id}`
    if (!this.logs.has(key)) this.logs.set(key, [])
    return this.logs.get(key)
  }

  /**
   * Append an event as the gateway itself would, assigning `seq` and `time`.
   *
   * This is the seam the tests write history through, and the same path §4
   * appends land on, so a contributor's event is indistinguishable from the
   * gateway's own once stored.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} type - the event type.
   * @param {object} data - the payload.
   * @param {number} [time] - epoch ms; defaults to now.
   * @returns {object} the stored envelope.
   */
  append(kind, id, type, data, time = Date.now()) {
    const events = this.log(kind, id)
    const envelope = { type, seq: events.length, time, data }
    events.push(envelope)
    this.broadcast(kind, id, envelope)
    return envelope
  }

  /**
   * Deliver one append to every subscriber, honouring {@link dropNextEvents}.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {object} event - the stored envelope.
   */
  broadcast(kind, id, event) {
    if (this.dropNextEvents > 0) {
      this.dropNextEvents -= 1
      return
    }
    for (const socket of this.sockets) socket.deliver(kind, id, event)
  }

  /**
   * Whether a request carries the app's bearer token.
   *
   * @param {import('node:http').IncomingMessage} request - the request.
   * @returns {boolean} true when authorized.
   */
  authorized(request) {
    return request.headers.authorization === `Bearer ${this.token}`
  }

  /**
   * Route one HTTP request.
   *
   * @param {import('node:http').IncomingMessage} request - the request.
   * @param {import('node:http').ServerResponse} response - the response.
   * @returns {Promise<void>} resolves once the response is written.
   */
  async handle(request, response) {
    const url = new URL(request.url, this.baseUrl || 'http://127.0.0.1')
    const send = (status, body) => {
      if (body === null) {
        response.writeHead(status)
        response.end()
        return
      }
      const text = JSON.stringify(body)
      response.writeHead(status, { 'content-type': 'application/json' })
      response.end(text)
    }
    if (!this.authorized(request)) return send(401, { code: 'unauthorized', error: 'bad token' })

    const parts = url.pathname.split('/').filter(Boolean)
    // api eventlog {kind} {id} events | projections {key} [schema]
    if (parts[0] !== 'api' || parts[1] !== 'eventlog' || parts.length < 5) {
      return send(404, { code: 'not_found', error: url.pathname })
    }
    const kind = decodeURIComponent(parts[2])
    const id = decodeURIComponent(parts[3])
    if (!this.contributions.units.includes(kind)) {
      return send(403, { code: 'unit_kind_not_granted', error: kind })
    }

    if (parts[4] === 'events' && request.method === 'GET') return this.readEvents(url, kind, id, send)
    if (parts[4] === 'events' && request.method === 'POST') {
      return this.appendEvent(await body(request), kind, id, send)
    }
    if (parts[4] === 'projections' && parts.length === 6 && request.method === 'POST') {
      return this.publishProjection(await body(request), kind, id, decodeURIComponent(parts[5]), send)
    }
    if (parts[4] === 'projections' && parts[6] === 'schema' && request.method === 'POST') {
      return this.publishSchema(await body(request), kind, id, decodeURIComponent(parts[5]), send)
    }
    return send(404, { code: 'not_found', error: url.pathname })
  }

  /**
   * `GET .../events` (§3).
   *
   * @param {URL} url - the request URL carrying `after` and `limit`.
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {(status: number, body: object | null) => void} send - the responder.
   * @returns {void}
   */
  readEvents(url, kind, id, send) {
    const after = Number(url.searchParams.get('after') ?? '-1')
    const limit = Number(url.searchParams.get('limit') ?? '500')
    if (!Number.isInteger(after)) return send(400, { code: 'invalid_after', error: String(after) })
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      return send(400, { code: 'invalid_limit', error: String(limit) })
    }
    const events = this.log(kind, id)
    const page = events.filter(event => event.seq > after).slice(0, limit)
    return send(200, { kind, id, events: page, lastSeq: events.length - 1 })
  }

  /**
   * `POST .../events` (§4).
   *
   * @param {object | null} payload - the request body.
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {(status: number, body: object | null) => void} send - the responder.
   * @returns {void}
   */
  appendEvent(payload, kind, id, send) {
    const type = payload && typeof payload.type === 'string' ? payload.type : ''
    if (!matches(type, this.contributions.events)) {
      return send(403, { code: 'event_type_not_owned', error: type })
    }
    const data = payload?.data ?? {}
    if (Buffer.byteLength(JSON.stringify(data)) > MAX_DATA_BYTES) {
      return send(413, { code: 'event_too_large', error: type })
    }
    return send(201, this.append(kind, id, type, data))
  }

  /**
   * `POST .../projections/{key}` (§5).
   *
   * @param {object | null} payload - the request body.
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} key - the projection key.
   * @param {(status: number, body: object | null) => void} send - the responder.
   * @returns {void}
   */
  publishProjection(payload, kind, id, key, send) {
    if (BUILTIN_KEYS.has(key) || !matches(key, this.contributions.projections)) {
      return send(403, { code: 'projection_key_not_owned', error: key })
    }
    if (!payload || !Object.hasOwn(payload, 'value')) {
      return send(400, { code: 'invalid_projection_value', error: key })
    }
    const seq = Number(payload.seq)
    const stateVersion = Number(payload.stateVersion)
    if (!Number.isInteger(seq) || !Number.isInteger(stateVersion)) {
      return send(400, { code: 'invalid_projection_value', error: key })
    }
    const rowKey = `${kind}/${id}/${key}`
    const stored = this.rows.get(rowKey)
    const newer = stored === undefined || stateVersion > stored.stateVersion || seq > stored.seq
    if (!newer) return send(409, { code: 'stale_seq', error: `stored seq ${stored.seq}` })
    this.rows.set(rowKey, { value: payload.value, seq, stateVersion })
    return send(204, null)
  }

  /**
   * `POST .../projections/{key}/schema` (§7).
   *
   * @param {object | null} payload - the schema.
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} key - the projection key.
   * @param {(status: number, body: object | null) => void} send - the responder.
   * @returns {void}
   */
  publishSchema(payload, kind, id, key, send) {
    if (!matches(key, this.contributions.projections)) {
      return send(403, { code: 'projection_key_not_owned', error: key })
    }
    this.schemas.set(`${kind}/${id}/${key}`, payload)
    return send(204, null)
  }

  /**
   * The published row for one unit and key.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} key - the projection key.
   * @returns {{value: unknown, seq: number, stateVersion: number} | undefined} the row.
   */
  row(kind, id, key) {
    return this.rows.get(`${kind}/${id}/${key}`)
  }

  /**
   * A `WebSocket`-shaped class bound to this gateway, for `ContractClient`'s
   * `socketImpl`.
   *
   * @returns {typeof FakeSocket} the socket class.
   */
  socketClass() {
    const gateway = this
    return class BoundSocket extends FakeSocket {
      /**
       * @param {string} url - ignored; the binding is the gateway instance.
       */
      constructor(url) {
        super(gateway, url)
      }
    }
  }
}

/**
 * Read a JSON request body.
 *
 * @param {import('node:http').IncomingMessage} request - the request.
 * @returns {Promise<object | null>} the parsed body, or null when absent or malformed.
 */
async function body(request) {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const text = Buffer.concat(chunks).toString('utf8')
  try {
    return text === '' ? null : JSON.parse(text)
  } catch {
    return null
  }
}

/**
 * The subscriber half of the fake: a minimal `WebSocket` surface that delivers
 * §3 frames in contract order.
 */
export class FakeSocket {
  /**
   * @param {FakeGateway} gateway - the gateway to subscribe against.
   * @param {string} url - the URL the client built; kept for assertions.
   */
  constructor(gateway, url) {
    this.gateway = gateway
    this.url = url
    this.readyState = 0
    this.listeners = new Map()
    /** @type {Set<string>} `kind/id` this socket is subscribed to. */
    this.subscriptions = new Set()
    gateway.sockets.add(this)
    // Opens on a later tick, like a real socket, so a caller that sends before
    // the open event is exercising the same ordering it would in production.
    setTimeout(() => {
      this.readyState = 1
      this.fire('open', {})
    }, 0)
  }

  /**
   * Register a listener.
   *
   * @param {string} type - the event name.
   * @param {(event: object) => void} listener - the handler.
   * @returns {void}
   */
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type).add(listener)
  }

  /**
   * Fire one event at its listeners.
   *
   * @param {string} type - the event name.
   * @param {object} event - the event object.
   * @returns {void}
   */
  fire(type, event) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  /**
   * Receive a client frame.
   *
   * @param {string} text - the serialized frame.
   * @returns {void}
   */
  send(text) {
    applyClientFrame(this.gateway, this, text, frame => this.fire('message', { data: JSON.stringify(frame) }))
  }

  /**
   * Deliver one append to this socket if it is subscribed.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {object} event - the stored envelope.
   * @returns {void}
   */
  deliver(kind, id, event) {
    if (this.readyState !== 1 || !this.subscriptions.has(`${kind}/${id}`)) return
    this.fire('message', {
      data: JSON.stringify({ type: 'eventlog_event', data: { kind, id, event } }),
    })
  }

  /** Close the socket, as the gateway would for a slow subscriber. */
  close() {
    if (this.readyState === 3) return
    this.readyState = 3
    this.gateway.sockets.delete(this)
    this.fire('close', {})
  }
}
