import { describe, it, expect, vi, afterEach } from 'vitest'
import { buildHtmlPopout, openHtmlInNewTab } from './htmlPopout'

describe('buildHtmlPopout', () => {
  it('hosts the untrusted file inside a null-origin allow-scripts iframe, not at top level', () => {
    const host = buildHtmlPopout('<script>zzqPwn()</script><div>zzq diagram</div>', 'Report')
    const doc = new DOMParser().parseFromString(host, 'text/html')

    const frame = doc.querySelector('iframe')
    expect(frame).not.toBeNull()
    // Scripts run (so CDN-loaded Mermaid etc. render) …
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    // … but the frame is null-origin: no allow-same-origin, so it cannot reach
    // the dashboard's localStorage/cookies even though it opens on a blob: URL
    // that inherits the dashboard origin at the TOP level.
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-same-origin')
    // The untrusted markup lives in the child's srcdoc, escaped by DOM
    // serialization — never as the top-level document.
    expect(frame?.getAttribute('srcdoc')).toContain('zzq diagram')
    // Top-level document carries only the fixed chrome, not the raw markup.
    expect(doc.body.querySelector('script')).toBeNull()
    expect(doc.title).toBe('Report')
  })

  it('omits the title tag when no title is given', () => {
    const host = buildHtmlPopout('<p>zzq</p>')
    expect(host).not.toContain('<title>')
  })

  it('cannot break the srcdoc attribute out with a crafted quote', () => {
    const host = buildHtmlPopout('"><img src=x onerror=zzqPwn()>')
    const doc = new DOMParser().parseFromString(host, 'text/html')
    // The payload stays inside the iframe's srcdoc; it did not inject a sibling
    // <img> into the host body.
    expect(doc.body.querySelector('img')).toBeNull()
    const frame = doc.querySelector('iframe')
    expect(frame?.getAttribute('srcdoc')).toContain('onerror=zzqPwn()')
  })
})

describe('openHtmlInNewTab', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('opens the host page as a blob url in a new tab with noopener', () => {
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:zzq')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const open = vi.spyOn(window, 'open').mockReturnValue(null)

    openHtmlInNewTab('<div>zzq</div>', 'Report')

    expect(open).toHaveBeenCalledWith('blob:zzq', '_blank', 'noopener')
    const blobArg = createObjectURL.mock.calls[0][0] as Blob
    expect(blobArg.type).toBe('text/html')
  })

  it('revokes the blob url after a delay so the new tab can fetch it first', () => {
    vi.useFakeTimers()
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:zzq')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(window, 'open').mockReturnValue(null)

    openHtmlInNewTab('<div>zzq</div>')
    expect(revoke).not.toHaveBeenCalled()
    vi.advanceTimersByTime(60_000)
    expect(revoke).toHaveBeenCalledWith('blob:zzq')
    vi.useRealTimers()
  })
})
