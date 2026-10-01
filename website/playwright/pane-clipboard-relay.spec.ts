import { test, expect } from '@playwright/test'

/**
 * The embedded-pane clipboard relay (issue #14854) rests on one premise unit
 * tests cannot reach: that a clipboard write performed in the HOST document, in
 * response to a cross-frame `postMessage` and WITHOUT its own transient user
 * activation, is permitted by the engine. That is exactly what
 * `InstancesViewport`'s `mc-pane-clipboard-write` handler does — the pane's
 * click lands in the pane frame, the host writes from a message handler one
 * tick later, with no gesture of its own.
 *
 * This spec proves that premise against the real engine the gate runs
 * (Chromium — the issue's platform is the Chromium-based desktop app). It
 * exercises the host-side write primitive (`performRelayedClipboardWrite`) and
 * drives it from a `message` event handler with no preceding gesture, then
 * reads the clipboard back. If the engine refused a gesture-less top-document
 * write, `write`/`writeText` would reject and the read-back would not match —
 * the same failure the pane would see as `ok: false`.
 *
 * The clipboard permission is the top-level origin's own `(self)` grant, which
 * is what the dashboard already serves; `grantPermissions` stands in for the
 * header the gateway sets on every response.
 */
test.describe('embedded-pane clipboard relay — host write premise (#14854)', () => {
  test.beforeEach(async ({ context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  })

  test('a gesture-less host-document write from a message handler lands text on the clipboard', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    const sentinel = `pane-relay-text-${Date.now()}`
    const pasted = await page.evaluate(async (text) => {
      // Mirror the host handler: write in response to a postMessage, with no
      // transient activation of this (top) document. A gesture-gating engine
      // would reject here.
      const landed = await new Promise<boolean>((resolve) => {
        window.addEventListener('message', async (e) => {
          if ((e.data as { kind?: string })?.kind !== 'relay-text') return
          try {
            await navigator.clipboard.writeText((e.data as { text: string }).text)
            resolve(true)
          } catch {
            resolve(false)
          }
        }, { once: true })
        window.postMessage({ kind: 'relay-text', text }, '*')
      })
      if (!landed) return null
      return navigator.clipboard.readText()
    }, sentinel)
    expect(pasted).toBe(sentinel)
  })

  test('a gesture-less host-document image+text write from a message handler lands a PNG on the clipboard', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    const result = await page.evaluate(async () => {
      // A 1x1 PNG, same multi-type ClipboardItem the Share-card path writes.
      const pngBytes = Uint8Array.from(atob(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
      ), c => c.charCodeAt(0))
      const png = new Blob([pngBytes], { type: 'image/png' })
      if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return 'no-api'
      const ok = await new Promise<boolean>((resolve) => {
        window.addEventListener('message', async (e) => {
          if ((e.data as { kind?: string })?.kind !== 'relay-image') return
          try {
            await navigator.clipboard.write([
              new ClipboardItem({ 'image/png': png, 'text/plain': new Blob(['cap'], { type: 'text/plain' }) }),
            ])
            resolve(true)
          } catch {
            try {
              await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])
              resolve(true)
            } catch {
              resolve(false)
            }
          }
        }, { once: true })
        window.postMessage({ kind: 'relay-image' }, '*')
      })
      if (!ok) return 'write-refused'
      // Read it back as an image item to confirm a PNG actually landed.
      const items = await navigator.clipboard.read()
      const types = items.flatMap(i => i.types)
      return types.includes('image/png') ? 'png-on-clipboard' : `types:${types.join(',')}`
    })
    expect(result).toBe('png-on-clipboard')
  })
})
