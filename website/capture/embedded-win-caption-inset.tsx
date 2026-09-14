/**
 * Capture entry for #10774 -- the notification bell rendered under the Windows
 * caption close button in an embedded remote-instance pane.
 *
 * A Windows desktop-app window paints its native min/max/close buttons in a
 * ~138px titleBarOverlay strip at the window's TOP-RIGHT. The top-level SPA
 * clears that strip with `.win-electron header.topbar-glass{padding-right:142px}`.
 * But a REMOTE dashboard shown in an InstancesViewport <iframe> pane has no
 * Electron preload, so `isWinElectron` is false inside it and `.win-electron`
 * never applies -- the bell and its unread badge land under the close button and
 * cannot be clicked. The fix relays the parent's platform down the host model
 * and toggles `.embedded-win-inset` on the pane's <html>, giving the pane header
 * the same 142px right reserve.
 *
 * This harness renders the REAL shipped `.tb-right` actions group (verbatim from
 * the header) inside a `header.topbar-glass`, under the real `index.css`, and
 * paints a 138px stand-in for the native caption overlay at the top-right so the
 * overlap (or its absence) is visible in the frame. The `.embedded-win-inset`
 * class is toggled exactly as EmbeddedHostBridge toggles it at runtime.
 *
 *   ?inset=off  the BEFORE state -- no .embedded-win-inset, bell under the strip
 *   ?inset=on   the AFTER state -- .embedded-win-inset applied, bell clear
 *
 * window.__measure() reports the bell button's right edge and whether it clears
 * the caption band, so the runner can ASSERT the before/after delta rather than
 * trusting the pixels.
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { Bell, Settings, Plus } from 'lucide-react'

import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const inset = params.get('inset') === 'on'
// The width the Windows titleBarOverlay reserves for min/max/close at default
// DPI -- mirrors WIN_CAPTION_OVERLAY_WIDTH in src/lib/electron.ts.
const CAPTION_BAND_PX = 138

function Header() {
  useEffect(() => {
    // Exactly what EmbeddedHostBridge does when the parent relays winInset.
    document.documentElement.classList.toggle('embedded-win-inset', inset)
    document.documentElement.dataset.theme = 'dark'
    // Expose the geometry the runner asserts on.
    ;(window as unknown as { __measure: () => unknown }).__measure = () => {
      const bell = document.querySelector('[data-testid="bell"]') as HTMLElement | null
      const header = document.querySelector('header.topbar-glass') as HTMLElement | null
      if (!bell || !header) return { ok: false }
      const b = bell.getBoundingClientRect()
      const h = header.getBoundingClientRect()
      // The caption band occupies the rightmost CAPTION_BAND_PX of the header.
      const bandLeft = h.right - CAPTION_BAND_PX
      return {
        ok: true,
        bellRight: Math.round(b.right),
        headerRight: Math.round(h.right),
        bandLeft: Math.round(bandLeft),
        // True when the bell (badge included) is entirely left of the caption
        // band -- i.e. clear of the native close button.
        clearsCaption: b.right <= bandLeft,
        paddingRight: getComputedStyle(header).paddingRight,
      }
    }
  }, [])

  return (
    // The pane document: full width, a caption band drawn at the top-right to
    // stand in for the parent window's native overlay.
    <div style={{ position: 'relative', width: 720, background: 'var(--bg)' }}>
      {/* Stand-in for the Windows native caption overlay (min/max/close). It is
          painted OVER the header's right edge, exactly where the OS controls sit. */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          right: 0,
          height: 42,
          width: CAPTION_BAND_PX,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-around',
          background: 'rgba(120,120,140,0.18)',
          color: 'var(--muted)',
          fontFamily: 'system-ui',
          fontSize: 13,
          pointerEvents: 'none',
          zIndex: 5,
        }}
      >
        <span>&#x2013;</span>
        <span>&#x25a1;</span>
        <span>&#x2715;</span>
      </div>
      <header className="topbar-glass" style={{ height: 42, display: 'flex', alignItems: 'center', paddingLeft: 20 }}>
        <div style={{ flex: 1 }} />
        {/* The shipped actions group, verbatim class names + the bell's -top-1
            -right-1 badge overhang, so the clip/inset behaviour is real. */}
        <div className="tb-right">
          <button type="button" aria-label="New"><Plus size={18} /></button>
          <button type="button" aria-label="Settings"><Settings size={18} /></button>
          <button type="button" aria-label="Notifications" data-testid="bell" className="relative">
            <Bell size={18} />
            <span
              className="absolute -top-1 -right-1"
              style={{
                minWidth: 16,
                height: 16,
                padding: '0 4px',
                borderRadius: 999,
                background: 'var(--accent)',
                color: '#fff',
                fontSize: 10,
                lineHeight: '16px',
                textAlign: 'center',
              }}
            >
              1
            </span>
          </button>
        </div>
      </header>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Header />)
