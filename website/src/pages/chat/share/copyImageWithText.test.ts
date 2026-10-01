/**
 * `copyImageWithText` and its host relay. Inside an embedded instance pane the
 * async image write is refused (the dashboard's `(self)` clipboard
 * Permissions-Policy does not reach the pane's cross-origin frame) and there is
 * no `execCommand` path for a PNG, so the helper relays the image to the host
 * frame, which holds the grant. Outside a pane the relay resolves false and the
 * caller falls back to a download.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock('../../../lib/paneClipboard', () => ({ writeClipboardViaHost: vi.fn() }))
import { writeClipboardViaHost } from '../../../lib/paneClipboard'
import { copyImageWithText } from './shareSupport'

describe('copyImageWithText', () => {
  let write: ReturnType<typeof vi.fn>
  const png = new Blob(['x'], { type: 'image/png' })

  beforeEach(() => {
    vi.clearAllMocks()
    write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write } })
    vi.stubGlobal('ClipboardItem', class { constructor(public items: unknown) {} })
    vi.mocked(writeClipboardViaHost).mockResolvedValue(false)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('writes a multi-type ClipboardItem locally and does not relay', async () => {
    await expect(copyImageWithText(png, 'cap')).resolves.toBe(true)
    expect(write).toHaveBeenCalledTimes(1)
    expect(writeClipboardViaHost).not.toHaveBeenCalled()
  })

  it('retries image-only locally before any relay', async () => {
    write.mockRejectedValueOnce(new Error('multi rejected')).mockResolvedValueOnce(undefined)
    await expect(copyImageWithText(png, 'cap')).resolves.toBe(true)
    expect(write).toHaveBeenCalledTimes(2)
    expect(writeClipboardViaHost).not.toHaveBeenCalled()
  })

  it('relays the image to the host when both local writes are refused (pane case)', async () => {
    write.mockRejectedValue(new Error('permissions policy'))
    vi.mocked(writeClipboardViaHost).mockResolvedValue(true)
    await expect(copyImageWithText(png, 'cap')).resolves.toBe(true)
    expect(writeClipboardViaHost).toHaveBeenCalledWith({ image: { png, text: 'cap' } })
  })

  it('relays when there is no ClipboardItem/write at all (older browser or pane)', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {} })
    vi.mocked(writeClipboardViaHost).mockResolvedValue(true)
    await expect(copyImageWithText(png, 'cap')).resolves.toBe(true)
    expect(writeClipboardViaHost).toHaveBeenCalledWith({ image: { png, text: 'cap' } })
  })

  it('resolves false (caller downloads) when neither local write nor the host relay works', async () => {
    write.mockRejectedValue(new Error('refused'))
    vi.mocked(writeClipboardViaHost).mockResolvedValue(false)
    await expect(copyImageWithText(png, 'cap')).resolves.toBe(false)
  })
})
