import { describe, expect, it } from 'vitest'
import reducer, { sideHydrate, sseSideResult, sideClose } from '../chatSlice'

// #22869: after a page reload the panel's transcript + queue (page-only store)
// are empty while the gateway still holds the side conversation. `sideHydrate`
// merges the gateway's held rows back in, idempotently with the SSE frames of an
// in-flight turn.

const slot = 's1'
const cold = () => reducer(undefined, { type: '@@INIT' })
const side = (state: ReturnType<typeof cold>) => state.slotSide[slot]

const read = (over: Partial<Parameters<typeof sideHydrate>[0]> = {}) =>
  sideHydrate({
    slot,
    open: true,
    messages: [
      { role: 'user', content: 'pineapple?', ts: '2026-10-21T20:00:00Z', run_id: 'r1' },
      { role: 'assistant', content: 'pineapple.', ts: '2026-10-21T20:00:01Z', run_id: 'r1' },
    ],
    queue: [],
    running: false,
    last_run_id: 'r1',
    created_at: '2026-10-21T20:00:00Z',
    ...over,
  })

describe('sideHydrate (#22869 reload rehydrate)', () => {
  it('fills an empty panel from the gateway rows', () => {
    const s = side(reducer(cold(), read()))
    expect(s.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'pineapple?'],
      ['assistant', 'pineapple.'],
    ])
    expect(s.lastRunId).toBe('r1')
    expect(s.pending).toBeFalsy()
  })

  it('is idempotent — a second hydrate adds nothing', () => {
    let state = reducer(cold(), read())
    state = reducer(state, read())
    expect(side(state).messages).toHaveLength(2)
  })

  it('does not duplicate a row an SSE frame already landed', () => {
    // An in-flight turn's frames arrived first (reload mid-turn), then the read lands.
    let state = reducer(cold(), sseSideResult({ slot, run_id: 'r1', role: 'user', content: 'pineapple?', ts: 1792742400 }))
    state = reducer(state, sseSideResult({ slot, run_id: 'r1', role: 'assistant', content: 'pineapple.', ts: 1792742401, final: true }))
    state = reducer(state, read())
    const s = side(state)
    expect(s.messages.filter(m => m.content === 'pineapple?')).toHaveLength(1)
    expect(s.messages.filter(m => m.content === 'pineapple.')).toHaveLength(1)
  })

  it('does not resurrect a side the user closed this session', () => {
    let state = reducer(cold(), sideClose(slot))
    state = reducer(state, read())
    expect(side(state)).toBeUndefined()
  })

  it('merges the queue and marks a running turn busy', () => {
    const s = side(reducer(cold(), read({
      queue: [{ id: 'q1', content: 'next one', ts: '2026-10-21T20:00:02Z' }],
      running: true,
    })))
    expect(s.queue?.map(q => q.content)).toEqual(['next one'])
    expect(s.pending).toBe(true)
    expect(s.streaming).toBe(true)
  })

  it('merges nothing when the gateway holds no sidecar', () => {
    const state = reducer(cold(), read({ open: false, messages: [], queue: [] }))
    expect(side(state)).toBeUndefined()
  })
})
