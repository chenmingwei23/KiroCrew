/**
 * The embedded-pane text-copy path. When the async Clipboard API is refused
 * (the dashboard's `(self)` clipboard Permissions-Policy does not reach a
 * pane's cross-origin frame), `copyWithOutcome` asks the host frame to write in
 * its top-level document BEFORE the textarea fallback — the fallback silently
 * fails for a menu-driven copy whose focus moves off the staging textarea, and
 * the host write does not depend on this frame's focus. Outside a pane the
 * relay resolves false and the fallback runs exactly as before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('../lib/paneClipboard', () => ({ writeClipboardViaHost: vi.fn() }))
import { writeClipboardViaHost } from '../lib/paneClipboard'
import { copyToClipboard, copyWithOutcome } from '../utils/clipboard'

describe('copyWithOutcome — host relay before the textarea fallback', () => {
  let writeText: ReturnType<typeof vi.fn>
  let execCommand: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    writeText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    execCommand = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
    vi.mocked(writeClipboardViaHost).mockResolvedValue(false)
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('does not relay when the async write succeeds', async () => {
    writeText.mockResolvedValue(undefined)
    await expect(copyToClipboard('ok')).resolves.toBe(true)
    expect(writeClipboardViaHost).not.toHaveBeenCalled()
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('relays to the host when the async write is refused, and does not touch the fallback on success', async () => {
    writeText.mockRejectedValue(new Error('permissions policy'))
    vi.mocked(writeClipboardViaHost).mockResolvedValue(true)
    const outcome = await copyWithOutcome('link')
    expect(outcome.ok).toBe(true)
    expect(outcome.hadAsyncApi).toBe(true)
    expect(writeClipboardViaHost).toHaveBeenCalledWith({ text: 'link' })
    // The host wrote it; the staging-textarea fallback never ran.
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('falls back to execCommand when the host has no answer (no bridge / top level)', async () => {
    writeText.mockRejectedValue(new Error('permissions policy'))
    vi.mocked(writeClipboardViaHost).mockResolvedValue(false)
    await expect(copyToClipboard('link')).resolves.toBe(true)
    expect(writeClipboardViaHost).toHaveBeenCalledWith({ text: 'link' })
    expect(execCommand).toHaveBeenCalledWith('copy')
  })

  it('reports false when neither the host nor the fallback copies', async () => {
    writeText.mockRejectedValue(new Error('permissions policy'))
    vi.mocked(writeClipboardViaHost).mockResolvedValue(false)
    execCommand.mockReturnValue(false)
    await expect(copyToClipboard('link')).resolves.toBe(false)
  })

  it('does not relay on a non-secure origin where there is no async API to refuse', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    await expect(copyToClipboard('link')).resolves.toBe(true)
    expect(writeClipboardViaHost).not.toHaveBeenCalled()
    expect(execCommand).toHaveBeenCalledWith('copy')
  })
})
