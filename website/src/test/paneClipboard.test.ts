/**
 * The embedded-pane clipboard-write relay. A pane cannot run
 * navigator.clipboard itself (the dashboard's `(self)` clipboard
 * Permissions-Policy does not reach its cross-origin frame), so it asks its
 * host frame to write and answer once. A host that never answers — an older
 * host, a plain browser parent — leaves the caller's own fallback to run.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('../lib/nativeNotify', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/nativeNotify')>()),
  relayTargetOrigin: vi.fn(() => HOST_ORIGIN),
}))
import { relayTargetOrigin } from '../lib/nativeNotify'
import {
  PANE_CLIPBOARD_RESULT_TYPE,
  PANE_CLIPBOARD_VERSION,
  PANE_CLIPBOARD_TIMEOUT_MS,
  parsePaneClipboardRequest,
  performRelayedClipboardWrite,
  writeClipboardViaHost,
} from '../lib/paneClipboard'

const HOST_ORIGIN = 'http://127.0.0.1:5476'

describe('writeClipboardViaHost — pane asks the host to write', () => {
  let parent: { postMessage: ReturnType<typeof vi.fn> }
  let restoreParent: () => void

  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(relayTargetOrigin).mockReturnValue(HOST_ORIGIN)
    parent = { postMessage: vi.fn() }
    const spy = vi.spyOn(window, 'parent', 'get').mockReturnValue(parent as unknown as Window)
    restoreParent = () => spy.mockRestore()
  })
  afterEach(() => {
    restoreParent()
    vi.useRealTimers()
  })

  const posted = () => parent.postMessage.mock.calls[0]?.[0] as { type: string; id: string; text?: string }
  const target = () => parent.postMessage.mock.calls[0]?.[1] as string
  const reply = (id: string, ok: boolean, o: { origin?: string; source?: unknown } = {}) =>
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: PANE_CLIPBOARD_RESULT_TYPE, v: PANE_CLIPBOARD_VERSION, id, ok },
      origin: o.origin ?? HOST_ORIGIN,
      source: (o.source ?? (parent as unknown)) as Window,
    }))

  it('posts the request to the host exact origin and resolves true on ok', async () => {
    const p = writeClipboardViaHost({ text: 'link' })
    expect(posted().type).toBe('mc-pane-clipboard-write')
    expect(posted().text).toBe('link')
    expect(target()).toBe(HOST_ORIGIN)
    expect(target()).not.toBe('*')
    reply(posted().id, true)
    await expect(p).resolves.toBe(true)
  })

  it('resolves false when the host reports the write failed', async () => {
    const p = writeClipboardViaHost({ text: 'x' })
    reply(posted().id, false)
    await expect(p).resolves.toBe(false)
  })

  it('resolves false after the timeout when the host never answers', async () => {
    const p = writeClipboardViaHost({ text: 'x' })
    vi.advanceTimersByTime(PANE_CLIPBOARD_TIMEOUT_MS)
    await expect(p).resolves.toBe(false)
  })

  it('ignores a reply from the wrong frame, the wrong origin, another write, or a bad version', async () => {
    const p = writeClipboardViaHost({ text: 'x' })
    const id = posted().id
    reply(id, true, { source: window })                 // not our parent
    reply(id, true, { origin: 'http://127.0.0.1:9999' }) // not the host origin
    reply('another-write', true)                         // not this write
    window.dispatchEvent(new MessageEvent('message', {   // unknown version
      data: { type: PANE_CLIPBOARD_RESULT_TYPE, v: 2, id, ok: true },
      origin: HOST_ORIGIN, source: parent as unknown as Window,
    }))
    vi.advanceTimersByTime(PANE_CLIPBOARD_TIMEOUT_MS)
    await expect(p).resolves.toBe(false)
  })

  it('resolves false immediately when there is no host to ask (not embedded / no referrer)', async () => {
    vi.mocked(relayTargetOrigin).mockReturnValue(null)
    await expect(writeClipboardViaHost({ text: 'x' })).resolves.toBe(false)
    expect(parent.postMessage).not.toHaveBeenCalled()
  })

  it('resolves false when postMessage throws', async () => {
    parent.postMessage.mockImplementation(() => { throw new Error('zzq') })
    await expect(writeClipboardViaHost({ text: 'x' })).resolves.toBe(false)
  })
})

describe('parsePaneClipboardRequest — host validates the untrusted payload', () => {
  const base = { type: 'mc-pane-clipboard-write', v: 1, id: 'c-1' }

  it('accepts a text request', () => {
    expect(parsePaneClipboardRequest({ ...base, text: 'hi' })).toEqual({ id: 'c-1', request: { text: 'hi' } })
  })

  it('accepts an image+text request whose png is a Blob', () => {
    const png = new Blob(['x'], { type: 'image/png' })
    expect(parsePaneClipboardRequest({ ...base, image: { png, text: 'cap' } })).toEqual({
      id: 'c-1', request: { image: { png, text: 'cap' } },
    })
  })

  it('rejects neither-shape, both-shapes, a non-Blob png, and bad id/type/version', () => {
    expect(parsePaneClipboardRequest(null)).toBeNull()
    expect(parsePaneClipboardRequest({ ...base })).toBeNull()                       // neither
    expect(parsePaneClipboardRequest({ ...base, text: 't', image: { png: new Blob(['x']), text: 'c' } })).toBeNull() // both
    expect(parsePaneClipboardRequest({ ...base, image: { png: 'not-a-blob', text: 'c' } })).toBeNull()
    expect(parsePaneClipboardRequest({ ...base, image: { png: new Blob(['x']), text: 7 } })).toBeNull()
    expect(parsePaneClipboardRequest({ ...base, text: 7 })).toBeNull()
    expect(parsePaneClipboardRequest({ ...base, type: 'mc-cursor-away', text: 't' })).toBeNull()
    expect(parsePaneClipboardRequest({ ...base, v: 2, text: 't' })).toBeNull()
    expect(parsePaneClipboardRequest({ type: 'mc-pane-clipboard-write', v: 1, text: 't' })).toBeNull() // no id
    expect(parsePaneClipboardRequest({ ...base, id: 'x'.repeat(65), text: 't' })).toBeNull()           // id too long
  })
})

describe('performRelayedClipboardWrite — host writes in its own document', () => {
  let writeText: ReturnType<typeof vi.fn>
  let write: ReturnType<typeof vi.fn>

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined)
    write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText, write } })
    vi.stubGlobal('ClipboardItem', class { constructor(public items: unknown) {} })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('writes text via writeText', async () => {
    await expect(performRelayedClipboardWrite({ text: 'hi' })).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('hi')
  })

  it('writes an image+text ClipboardItem via write', async () => {
    const png = new Blob(['x'], { type: 'image/png' })
    await expect(performRelayedClipboardWrite({ image: { png, text: 'c' } })).resolves.toBe(true)
    expect(write).toHaveBeenCalledTimes(1)
  })

  it('retries image-only when the multi-type image write is rejected', async () => {
    const png = new Blob(['x'], { type: 'image/png' })
    write.mockRejectedValueOnce(new Error('multi rejected')).mockResolvedValueOnce(undefined)
    await expect(performRelayedClipboardWrite({ image: { png, text: 'c' } })).resolves.toBe(true)
    expect(write).toHaveBeenCalledTimes(2)
  })

  it('resolves false when the write throws, when the API is absent, and for a shapeless request', async () => {
    writeText.mockRejectedValue(new Error('refused'))
    await expect(performRelayedClipboardWrite({ text: 'x' })).resolves.toBe(false)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    await expect(performRelayedClipboardWrite({ text: 'x' })).resolves.toBe(false)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    await expect(performRelayedClipboardWrite({})).resolves.toBe(false)
  })

  it('never reads the clipboard', async () => {
    const readText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText, write, readText } })
    await performRelayedClipboardWrite({ text: 'hi' })
    expect(readText).not.toHaveBeenCalled()
  })
})
