/**
 * Maps one member's log envelopes onto the event stream the hosted plugin
 * expects.
 *
 * The plugin folds an agent-loop transcript: a step opens, tokens stream, a
 * message assembles, the step closes. A member's log records something else —
 * config snapshots, DM messages, slot spans, patrol spans. Three of those
 * carry the shape the fold needs, and the rest carry nothing it can use:
 *
 * | member envelope   | plugin event                         |
 * | ----------------- | ------------------------------------ |
 * | `slot/opened`     | `step/start`                         |
 * | `member/message`  | `assistant/chunk` (a text delta)     |
 * | `slot/closed`     | `assistant/message` then `step/end`  |
 *
 * So the numbers the plugin then produces mean, for a member:
 * `steps` is fully observed slot spans, `turns` is distinct slots driven,
 * `llmMs` is total wall time holding a slot open, and `ttftMs` is time from
 * opening a slot to its first message. `toolMs`, `decodeMs` and `decodeTokens`
 * stay 0 — a member's log carries no tool pairs and no token usage, and the
 * adapter does not invent either.
 *
 * Two deliberate limits:
 *
 * - A `slot/closed` with no matching `slot/opened` in the stream emits
 *   nothing, so a span whose start was never observed is not counted.
 * - The fold tracks ONE open step, because the runtime it comes from has one
 *   agent loop. A member can hold two slots at once, which that model cannot
 *   represent, so a second `slot/opened` while one span is live is skipped and
 *   counted in {@link MapperState.overlapped} rather than silently replacing
 *   the live span (which would lose its wall time).
 *
 * Mapper state is folded state: it is dropped and rebuilt with the projection
 * whenever the log is re-read from the start.
 *
 * @module dsh_adapter/map
 */

/** Member envelope types this mapper reads; every other type maps to nothing. */
export const MAPPED_TYPES = Object.freeze(['slot/opened', 'slot/closed', 'member/message'])

/**
 * @typedef {object} MapperState
 * @property {Record<string, number>} turns - slot key to its assigned turn number.
 * @property {number} nextTurn - the turn number the next unseen slot key takes.
 * @property {number} steps - how many spans have opened, i.e. the next step number.
 * @property {{slotKey: string, turn: number, step: number} | null} open - the live span, if any.
 * @property {number} overlapped - `slot/opened` events skipped because a span was already live.
 */

/**
 * Fresh mapper state for an empty log.
 *
 * @returns {MapperState} the initial state.
 */
export function initMapper() {
  return { turns: {}, nextTurn: 1, steps: 0, open: null, overlapped: 0 }
}

/**
 * The turn number for a slot key, assigning one on first sight.
 *
 * Assignment follows first-seen order, so the same log always yields the same
 * numbering — the fold is only reproducible if this is.
 *
 * @param {MapperState} state - the mapper state, mutated to record a new key.
 * @param {string} slotKey - the slot key from the envelope.
 * @returns {number} the slot's turn number.
 */
function turnFor(state, slotKey) {
  const known = Object.hasOwn(state.turns, slotKey) ? state.turns[slotKey] : undefined
  if (known !== undefined) return known
  const assigned = state.nextTurn
  state.turns[slotKey] = assigned
  state.nextTurn += 1
  return assigned
}

/**
 * Translate one member envelope into zero or more plugin events.
 *
 * The returned events carry `seq: -1`: sequence numbers belong to the member
 * log, and one envelope can produce two plugin events, so a plugin-side seq
 * would be a fiction. The fold this drives reads `type`, `time` and `data`
 * only. `time` is carried through unchanged, which is what makes the wall
 * times real.
 *
 * @param {MapperState} state - the mapper state; mutated to track spans and turns.
 * @param {{type: string, seq: number, time: number, data: object}} envelope - one member log envelope.
 * @returns {Array<{type: string, seq: number, time: number, data: object}>} plugin events, in order.
 */
export function mapEnvelope(state, envelope) {
  const { type, time } = envelope
  const data = envelope.data ?? {}
  const at = (eventType, eventData) => ({ type: eventType, seq: -1, time, data: eventData })

  if (type === 'slot/opened') {
    const slotKey = typeof data.slot_key === 'string' ? data.slot_key : ''
    if (!slotKey) return []
    if (state.open !== null) {
      state.overlapped += 1
      return []
    }
    const turn = turnFor(state, slotKey)
    const step = state.steps + 1
    state.steps = step
    state.open = { slotKey, turn, step }
    return [at('step/start', { turn, step })]
  }

  if (type === 'member/message') {
    const open = state.open
    // An empty preview is not a token by the fold's own rule, so it would be
    // dropped downstream anyway; not emitting it keeps the streams identical.
    const preview = typeof data.preview === 'string' ? data.preview : ''
    if (open === null || preview === '') return []
    return [at('assistant/chunk', { turn: open.turn, step: open.step, chunk: { type: 'text-delta', text: preview } })]
  }

  if (type === 'slot/closed') {
    const open = state.open
    const slotKey = typeof data.slot_key === 'string' ? data.slot_key : ''
    if (open === null || (slotKey !== '' && slotKey !== open.slotKey)) return []
    state.open = null
    return [
      at('assistant/message', { turn: open.turn, step: open.step }),
      at('step/end', { turn: open.turn, step: open.step }),
    ]
  }

  return []
}

/**
 * Translate a run of envelopes in order.
 *
 * @param {MapperState} state - the mapper state; mutated across the run.
 * @param {Array<{type: string, seq: number, time: number, data: object}>} envelopes - envelopes in seq order.
 * @returns {Array<{type: string, seq: number, time: number, data: object}>} the concatenated plugin events.
 */
export function mapEnvelopes(state, envelopes) {
  const out = []
  for (const envelope of envelopes) out.push(...mapEnvelope(state, envelope))
  return out
}
