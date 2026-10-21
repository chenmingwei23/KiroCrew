/**
 * Evidence for the Side Chat reload rehydrate (#22869).
 *
 * THE CHANGE: the panel's transcript lives only in the page store, so a reload
 * empties it while the gateway still holds the conversation. SideChat now reads
 * `GET /api/chat/slots/{slot}/side` once on mount and merges the held rows back
 * in. Both scenes mount the REAL SideChat against the real store, stylesheet,
 * theme tokens and live i18n catalog, starting from an EMPTY store — exactly the
 * state right after a browser reload. The only difference is what the mocked
 * read route returns:
 *
 *   ?scene=before  the read answers `open: false` (today's behaviour, or no
 *                  sidecar): the panel stays empty, which is the bug.
 *   ?scene=after   the read answers the held conversation: the fix rehydrates
 *                  the panel so the user sees what the next question joins.
 *
 * The API is stubbed at window.fetch so the real client → sideRead → sideHydrate
 * path runs; no gateway exists here.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SideChat from '../src/pages/chat/SideChat'
import dashboardReducer from '../src/store/dashboardSlice'
import { createTestStore } from '../src/test/helpers'
import reducer from '../src/store/chatSlice'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'after' ? 'after' : 'before'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const SLOT = 'capture-slot'

/** The sidecar the gateway still holds for this slot. `before` returns the
 *  empty shape (no read route / no sidecar); `after` returns the conversation. */
const heldRead =
  scene === 'after'
    ? {
        ok: true,
        open: true,
        messages: [
          { role: 'user', content: 'Remember the word pineapple.', ts: '2026-10-21T20:00:00Z', run_id: 'r1' },
          { role: 'assistant', content: 'Got it — I\u2019ll remember the word pineapple for this side conversation.', ts: '2026-10-21T20:00:01Z', run_id: 'r1' },
        ],
        queue: [],
        running: false,
        last_run_id: 'r1',
        created_at: '2026-10-21T20:00:00Z',
      }
    : { ok: true, open: false, messages: [], queue: [], running: false }

const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  // The read route the panel hits on mount.
  if (/\/api\/chat\/slots\/[^/]+\/side$/.test(url)) return json(heldRead)
  // The footer's posture probe; answer so it renders its resting note.
  if (url.includes('/api/chat/side/tools')) return json({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false })
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}

// EMPTY store — the state right after a reload. The panel's own mount read is
// what fills it in the `after` scene.
const initial = reducer(undefined, { type: '@@INIT' })
const store = createTestStore({
  dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
  chat: { ...initial, activeSlot: SLOT },
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div
        data-capture-root
        style={{ width: 420, height: 560, margin: '0 auto', background: 'var(--bg)', border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
      >
        <SideChat slot={SLOT} />
      </div>
    </QueryClientProvider>
  </Provider>,
)
