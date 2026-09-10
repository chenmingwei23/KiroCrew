/**
 * Client for the contribution protocol: catch-up reads and the event stream
 * (§3), appends (§4), projection publishes (§5), and the render schema (§7).
 *
 * This is the only file that knows the wire. Everything above it works in
 * envelopes and values, so pointing the adapter at the fake gateway or the real
 * one is a change of base URL.
 *
 * Uses the global `fetch` and `WebSocket` Node ships, so the adapter's only
 * npm dependency is the one the hosted plugin itself declares.
 *
 * Authentication is a bearer token, per §2. Nothing in the gateway hands an app
 * backend such a token today — the backend environment carries a proxy secret
 * for verifying INBOUND forwarded requests and nothing for calling out — so the
 * token arrives by configuration and the caller decides what to do when it is
 * absent.
 *
 * @module dsh_adapter/contract
 */

/** Largest `limit` §3 accepts on a catch-up read. */
export const MAX_LIMIT = 500

/**
 * An error carrying the protocol's machine-readable `code` (§9).
 */
export class ContractError extends Error {
  /**
   * @param {number} status - the HTTP status.
   * @param {string} code - the protocol error code, or `''` when the body carried none.
   * @param {string} message - a human-readable description.
   */
  constructor(status, code, message) {
    super(message)
    this.name = 'ContractError'
    this.status = status
    this.code = code
  }
}

/**
 * Read one response, turning a protocol error body into a {@link ContractError}.
 *
 * @param {Response} response - the fetch response.
 * @returns {Promise<object | null>} the parsed body, or null for `204`.
 */
async function readBody(response) {
  if (response.status === 204) return null
  const text = await response.text()
  let body = null
  try {
    body = text === '' ? null : JSON.parse(text)
  } catch {
    body = null
  }
  if (!response.ok) {
    const code = body && typeof body.code === 'string' ? body.code : ''
    const detail = body && typeof body.error === 'string' ? body.error : text.slice(0, 200)
    throw new ContractError(response.status, code, `${response.status} ${code || 'unknown'}: ${detail}`)
  }
  return body
}

/**
 * A contribution-protocol client bound to one gateway and one app token.
 */
export class ContractClient {
  /**
   * @param {object} options - client options.
   * @param {string} options.baseUrl - gateway origin, e.g. `http://127.0.0.1:5476`.
   * @param {string} options.token - the app's bearer token (§2).
   * @param {typeof fetch} [options.fetchImpl] - override for tests.
   * @param {typeof WebSocket} [options.socketImpl] - override for tests.
   */
  constructor({ baseUrl, token, fetchImpl, socketImpl }) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
    this.token = token
    this.fetchImpl = fetchImpl ?? globalThis.fetch
    this.socketImpl = socketImpl ?? globalThis.WebSocket
  }

  /**
   * Authorization header for every call.
   *
   * @returns {Record<string, string>} the headers.
   */
  headers() {
    return { authorization: `Bearer ${this.token}`, 'content-type': 'application/json' }
  }

  /**
   * One page of a unit's log, oldest first, every `seq` greater than `after` (§3).
   *
   * @param {string} kind - the unit kind, e.g. `member`.
   * @param {string} id - the unit id.
   * @param {number} after - read events with a higher seq; `-1` starts at the beginning.
   * @param {number} [limit] - page size, 1..500.
   * @returns {Promise<{kind: string, id: string, events: Array<object>, lastSeq: number}>} the page.
   */
  async readEvents(kind, id, after, limit = MAX_LIMIT) {
    const query = new URLSearchParams({ after: String(after), limit: String(Math.min(limit, MAX_LIMIT)) })
    const url = `${this.baseUrl}/api/eventlog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/events?${query}`
    const response = await this.fetchImpl(url, { headers: this.headers() })
    return await readBody(response)
  }

  /**
   * Read a unit's whole log by paging until the page is short (§3).
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {number} [after] - resume point; `-1` reads from the beginning.
   * @returns {Promise<{events: Array<object>, lastSeq: number}>} every event after `after`, in order.
   */
  async readAll(kind, id, after = -1) {
    const events = []
    let cursor = after
    let lastSeq = after
    for (;;) {
      const page = await this.readEvents(kind, id, cursor, MAX_LIMIT)
      const batch = page.events ?? []
      events.push(...batch)
      lastSeq = typeof page.lastSeq === 'number' ? page.lastSeq : lastSeq
      if (batch.length === 0) return { events, lastSeq }
      cursor = batch[batch.length - 1].seq
      if (cursor >= lastSeq) return { events, lastSeq }
    }
  }

  /**
   * Append one event under the app's own namespace (§4).
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} type - the event type; must match a declared `events` pattern.
   * @param {object} data - the payload, at most 64 KiB serialized.
   * @returns {Promise<object>} the stored envelope, with the gateway's `seq` and `time`.
   */
  async appendEvent(kind, id, type, data) {
    const url = `${this.baseUrl}/api/eventlog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/events`
    const response = await this.fetchImpl(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({ type, data }),
    })
    return await readBody(response)
  }

  /**
   * Publish a whole projected value (§5).
   *
   * A `409 stale_seq` is the protocol's normal answer to a replay or a slower
   * contributor, so it is reported rather than thrown.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} key - the projection key; must match a declared `projections` pattern.
   * @param {unknown} value - the whole value.
   * @param {number} seq - the log position the value is current as of.
   * @param {number} stateVersion - the contributor's fold version.
   * @returns {Promise<{published: boolean, code: string}>} whether the row moved, and the code when it did not.
   */
  async publishProjection(kind, id, key, value, seq, stateVersion) {
    const url = `${this.baseUrl}/api/eventlog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`
      + `/projections/${encodeURIComponent(key)}`
    const response = await this.fetchImpl(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({ value, seq, stateVersion }),
    })
    if (response.status === 409) {
      const body = await response.json().catch(() => null)
      return { published: false, code: body && typeof body.code === 'string' ? body.code : 'stale_seq' }
    }
    await readBody(response)
    return { published: true, code: '' }
  }

  /**
   * Publish the declarative render schema for one key (§7).
   *
   * Optional by design: a gateway without the route leaves the view rendering
   * generically, so a `404` is reported, not thrown.
   *
   * @param {string} kind - the unit kind.
   * @param {string} id - the unit id.
   * @param {string} key - the projection key.
   * @param {object} schema - `{title, kind, fields}` per §7.
   * @returns {Promise<{published: boolean, status: number}>} whether the gateway accepted it.
   */
  async publishSchema(kind, id, key, schema) {
    const url = `${this.baseUrl}/api/eventlog/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`
      + `/projections/${encodeURIComponent(key)}/schema`
    const response = await this.fetchImpl(url, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(schema),
    })
    if (response.status === 404 || response.status === 405) return { published: false, status: response.status }
    await readBody(response)
    return { published: true, status: response.status }
  }

  /**
   * Open the app WebSocket and stream one unit's appends (§3).
   *
   * `eventlog_subscribed` arrives before any `eventlog_event` for the
   * subscription, so `onSubscribed` fires first and carries the `lastSeq` a
   * catch-up read must reach.
   *
   * @param {object} options - subscription options.
   * @param {string} options.kind - the unit kind.
   * @param {string} options.id - the unit id.
   * @param {(lastSeq: number) => void} options.onSubscribed - called once per (re)subscribe.
   * @param {(event: object) => void} options.onEvent - called once per streamed append.
   * @param {(reason: string) => void} [options.onClosed] - called when the socket closes or errors.
   * @returns {{close: () => void}} a handle that unsubscribes and closes.
   */
  subscribe({ kind, id, onSubscribed, onEvent, onClosed }) {
    const wsUrl = `${this.baseUrl.replace(/^http/, 'ws')}/api/apps/ws?token=${encodeURIComponent(this.token)}`
    const socket = new this.socketImpl(wsUrl)
    let closed = false

    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({ type: 'eventlog_subscribe', data: { kind, id } }))
    })
    socket.addEventListener('message', event => {
      let frame = null
      try {
        frame = JSON.parse(typeof event.data === 'string' ? event.data : '')
      } catch {
        return
      }
      const data = frame?.data ?? {}
      if (data.kind !== kind || data.id !== id) return
      if (frame.type === 'eventlog_subscribed') onSubscribed(typeof data.lastSeq === 'number' ? data.lastSeq : -1)
      else if (frame.type === 'eventlog_event' && data.event) onEvent(data.event)
    })
    const finish = reason => {
      if (closed) return
      closed = true
      onClosed?.(reason)
    }
    socket.addEventListener('close', () => finish('closed'))
    socket.addEventListener('error', () => finish('error'))

    return {
      close() {
        if (socket.readyState === 1) {
          try {
            socket.send(JSON.stringify({ type: 'eventlog_unsubscribe', data: { kind, id } }))
          } catch {
            // A socket that died between the readyState check and the send needs
            // no unsubscribe: the gateway drops the subscription with the
            // connection.
          }
        }
        closed = true
        socket.close()
      },
    }
  }
}
