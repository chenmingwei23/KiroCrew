/**
 * Open an untrusted, local HTML document in a real browser tab — origin-isolated.
 *
 * The dashboard's inline file preview renders local HTML inside a fully inert
 * `<iframe sandbox="">` (no script execution), which is correct for a preview
 * surface but means a document that pulls a CDN `<script>` (Mermaid, Chart.js,
 * …) shows its raw source instead of rendering. To let the user actually SEE
 * such a document, this pops it out into a new tab where scripts run natively.
 *
 * Security: the document is UNTRUSTED (agent-written or downloaded local
 * markup). A `blob:` URL inherits the dashboard's origin, so opening the raw
 * markup as a TOP-LEVEL document would let it read/clear the dashboard's
 * localStorage/cookies. So the popout never hosts the document directly — it
 * hosts a tiny fixed page whose only content is a `sandbox="allow-scripts"`
 * (null-origin) iframe, with the document inside its `srcdoc`. Scripts run; the
 * document can reach neither the dashboard origin nor the host filesystem. This
 * is the same origin-isolation the widget pop-out uses
 * (`apps/mochi/src/shared/widgetPopout.ts`).
 *
 * This module holds ONLY machine HTML/markup (a DOCTYPE, a fixed stylesheet, an
 * iframe element) — never user-facing copy — which is why it is listed in
 * `eslint.i18n.config.js`'s no-literal-string ignore set.
 */

/** Build the origin-isolated host page for `content`. */
export function buildHtmlPopout(content: string, title?: string): string {
  // The untrusted document is assigned to the iframe's `srcdoc` PROPERTY and the
  // element is serialized by the DOM (which escapes the attribute), so it cannot
  // break out of `srcdoc="…"`. The iframe is built DETACHED (never appended to a
  // document) so serializing it triggers no frame load. The surrounding chrome
  // is fixed, untrusted-free markup — no dynamic HTML-string sink.
  const frame = document.createElement('iframe')
  frame.setAttribute('sandbox', 'allow-scripts')
  frame.srcdoc = content

  let titleTag = ''
  if (title !== undefined) {
    const titleEl = document.createElement('title')
    titleEl.textContent = title
    titleTag = titleEl.outerHTML
  }

  return (
    '<!DOCTYPE html><html><head><meta charset="utf-8">' +
    titleTag +
    '<style>html,body{margin:0;height:100%}' +
    'iframe{border:0;width:100vw;height:100vh;display:block}</style>' +
    '</head><body>' +
    frame.outerHTML +
    '</body></html>'
  )
}

/** Open `content` in a new, origin-isolated browser tab. */
export function openHtmlInNewTab(content: string, title?: string): void {
  const url = URL.createObjectURL(new Blob([buildHtmlPopout(content, title)], { type: 'text/html' }))
  window.open(url, '_blank', 'noopener')
  // Revoked on a delay so the new tab has time to fetch it.
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
