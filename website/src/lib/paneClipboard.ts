/**
 * Clipboard writes performed in the HOST document on behalf of an embedded
 * instance pane.
 *
 * An instance pane is the full dashboard SPA inside a cross-origin <iframe>
 * (`InstancesViewport.srcFor`). The dashboard serves a `(self)` clipboard
 * Permissions-Policy (`dashboard/server.py` `_PERMISSIONS_POLICY`), and that
 * grant cannot be delegated to another origin, so the pane's `allow=
 * "clipboard-write"` has no effect and `navigator.clipboard.writeText` /
 * `.write` reject inside the pane. The `execCommand('copy')` textarea fallback
 * covers plain text only, and even that silently fails for a copy that runs
 * from a menu item whose focus handling moves focus off the staging textarea
 * before the fallback fires. Image copy has no `execCommand` path at all.
 *
 * The frame that DOES hold the `(self)` grant is the parent (its own top-level
 * origin). This module relays the payload there instead of widening the
 * security header: the pane posts an `mc-pane-clipboard-write` request to
 * `window.parent`, the parent (`InstancesViewport` onMessage) validates the
 * sender's origin against its currently-warm tunnel ports and performs the
 * write itself, then answers once with `mc-pane-clipboard-result`. A host that
 * never answers — an older host, a plain browser parent — leaves the caller to
 * its own `execCommand` fallback via the timeout below.
 *
 * `clipboard-read` is deliberately NOT bridged: read is the more sensitive
 * grant and this stays within the existing `clipboard-write` scope
 * (`InstancesViewport.tsx` documents why read is left undelegated).
 */
import { relayTargetOrigin } from './nativeNotify'

/** Pane -> host: perform this write in the top document. */
export const PANE_CLIPBOARD_WRITE_TYPE = 'mc-pane-clipboard-write'
/** Host -> pane: the one result, `{ ok: boolean }`. */
export const PANE_CLIPBOARD_RESULT_TYPE = 'mc-pane-clipboard-result'
export const PANE_CLIPBOARD_VERSION = 1

/**
 * How long the pane waits for the host before giving up and letting its own
 * fallback run. A host with the bridge answers in well under a frame, so this
 * only bounds the silence of an older host or a browser parent that will never
 * answer. Kept short: the Copy control is held inert for this whole wait, so a
 * pane copy against a bridge-less host must not feel like a dead button — a few
 * hundred ms is far above a real bridge's round trip and far below a stall a
 * user would read as broken.
 */
export const PANE_CLIPBOARD_TIMEOUT_MS = 400

/** Image payload for a one-item image+text write (the Share card's copy). */
export interface PaneClipboardImage {
  png: Blob
  text: string
}

/** The write a pane asks its host to perform: text, or an image+text item. */
export interface PaneClipboardRequest {
  text?: string
  image?: PaneClipboardImage
}

let relaySeq = 0

/**
 * Ask the host frame to perform `request` on the top-level clipboard. Resolves
 * `true` only once the host reports the write landed, `false` on a host failure
 * or no host to ask (not embedded, or the browser withholds the referrer so
 * there is no exact origin to address). Never rejects.
 *
 * The payload crosses a trust boundary in BOTH directions: the pane addresses
 * the exact loopback host origin (never `'*'`), and the host independently
 * validates the sender's origin before writing. Only the host's reply about
 * THIS request, from the frame and origin we addressed, is accepted.
 */
export function writeClipboardViaHost(request: PaneClipboardRequest): Promise<boolean> {
  const target = relayTargetOrigin()
  if (target === null) return Promise.resolve(false)
  const parent = window.parent
  if (!parent || parent === window) return Promise.resolve(false)

  const id = `${Date.now().toString(36)}-${(relaySeq += 1)}`
  return new Promise<boolean>(resolve => {
    let done = false
    let timer: number | undefined

    const finish = (ok: boolean) => {
      if (done) return
      done = true
      if (timer !== undefined) window.clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      resolve(ok)
    }

    function onMessage(e: MessageEvent) {
      // Only our own host frame, speaking from the origin we addressed, about
      // THIS write. A sibling frame or a stale reply to an earlier write is
      // ignored rather than allowed to resolve the wrong request.
      if (done || e.source !== parent || e.origin !== target) return
      const data = e.data as { type?: unknown; v?: unknown; id?: unknown; ok?: unknown } | null
      if (!data || typeof data !== 'object') return
      if (data.v !== PANE_CLIPBOARD_VERSION || data.id !== id) return
      if (data.type === PANE_CLIPBOARD_RESULT_TYPE && typeof data.ok === 'boolean') finish(data.ok)
    }

    window.addEventListener('message', onMessage)
    try {
      parent.postMessage(
        { type: PANE_CLIPBOARD_WRITE_TYPE, v: PANE_CLIPBOARD_VERSION, id, ...request },
        target,
      )
    } catch {
      finish(false)
      return
    }
    // A host without the bridge never answers; fall through to the caller's own
    // fallback rather than leaving the promise pending forever.
    timer = window.setTimeout(() => finish(false), PANE_CLIPBOARD_TIMEOUT_MS)
  })
}

/**
 * Narrow an untrusted `postMessage` payload to a clipboard-write request.
 * Returns null unless it is a well-formed request carrying EITHER a text string
 * OR an image whose `png` is a Blob and whose `text` is a string — never both
 * shapes at once and never neither. Origin is NOT checked here: the caller
 * (`InstancesViewport` onMessage) has already resolved `event.origin` to a warm
 * tunnel before it reaches this.
 */
export function parsePaneClipboardRequest(
  data: unknown,
): { id: string; request: PaneClipboardRequest } | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (d.type !== PANE_CLIPBOARD_WRITE_TYPE || d.v !== PANE_CLIPBOARD_VERSION) return null
  if (typeof d.id !== 'string' || !d.id || d.id.length > 64) return null
  const hasText = typeof d.text === 'string'
  const img = d.image && typeof d.image === 'object' ? (d.image as Record<string, unknown>) : null
  const hasImage = !!img && img.png instanceof Blob && typeof img.text === 'string'
  // Exactly one shape: a request that is both or neither is malformed.
  if (hasText === hasImage) return null
  if (hasImage) {
    return { id: d.id, request: { image: { png: img!.png as Blob, text: img!.text as string } } }
  }
  return { id: d.id, request: { text: d.text as string } }
}

/**
 * Host side: perform a pane's relayed clipboard write in THIS (top-level)
 * document, where the `(self)` Permissions-Policy grants `clipboard-write`.
 * Resolves whether the write landed; never rejects. An image+text request uses
 * the async `ClipboardItem` write (the only path that carries a PNG), with an
 * image-only retry mirroring `copyImageWithText`; a text request uses
 * `writeText`. Only `write`/`writeText` is used here — read is never performed
 * on a pane's behalf.
 */
export async function performRelayedClipboardWrite(request: PaneClipboardRequest): Promise<boolean> {
  const clip = navigator.clipboard
  if (!clip) return false
  try {
    if (request.image) {
      if (typeof ClipboardItem === 'undefined' || !clip.write) return false
      const { png, text } = request.image
      try {
        await clip.write([
          new ClipboardItem({ 'image/png': png, 'text/plain': new Blob([text], { type: 'text/plain' }) }),
        ])
        return true
      } catch {
        await clip.write([new ClipboardItem({ 'image/png': png })])
        return true
      }
    }
    if (typeof request.text === 'string' && clip.writeText) {
      await clip.writeText(request.text)
      return true
    }
    return false
  } catch {
    return false
  }
}
