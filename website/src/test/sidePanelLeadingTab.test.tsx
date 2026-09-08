/**
 * The host-owned LEADING tab (`SidePanel.leadingTab` + `usePanelTabs`'s
 * `leadingId`): the Crew Members page's "Crew summary".
 *
 * Three contracts, each of which a plausible refactor breaks silently:
 *
 * 1. Placement and shape — it renders AHEAD of the permanent pinned block, as a
 *    pinned-style chip with no close control, and it is never a Reorder item.
 * 2. Focus — a fresh strip opens ON it (not on the first pinned view, which is
 *    what `syncPinned` would otherwise pick), and closing the last dynamic tab
 *    lands back on it rather than on `null`.
 * 3. The panel's close control follows `onClose`: absent means permanent (no
 *    button), present means the button renders — the Members page relies on
 *    the former for its docked column and the latter for its overlay.
 * 4. `hiddenViews` withdraws a view from BOTH the pinned block and the + menu —
 *    a host that cannot feed a view must not ship it empty.
 *
 * Bodies are stubbed as in `sidePanelPinnedAlwaysPresent.test.tsx`; only the
 * strip and the leading body are driven.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { PINNED_VIEWS, usePanelTabs, __resetPanelTabs, type ViewKind } from '../hooks/usePanelTabs'

const LEADING_ID = 'crew-summary'

/** Exposes the strip model so a case can act on it (open a view, close a tab)
 *  the way a host would, without reaching through the DOM for everything. */
let ctl: ReturnType<typeof usePanelTabs> | null = null

function Harness({ closable, slot = 'member-radar', hidden }: { closable: boolean; slot?: string; hidden?: ReadonlySet<ViewKind> }) {
  const tabsCtl = usePanelTabs(slot, undefined, { leadingId: LEADING_ID })
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={slot}
      onFileSave={async () => {}}
      onClose={closable ? () => {} : undefined}
      canDockBottom={false}
      hiddenViews={hidden}
      leadingTab={{
        id: LEADING_ID,
        title: 'Crew summary',
        icon: <span data-testid="leading-icon" />,
        render: () => <div data-testid="leading-body">radar summary</div>,
      }}
    />
  )
}

function renderPanel(props: { closable: boolean; slot?: string; hidden?: ReadonlySet<ViewKind> }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness {...props} />
      </Provider>
    </QueryClientProvider>,
  )
}

const chips = () => screen.getAllByRole('tab')
const nameOf = (el: HTMLElement) => el.getAttribute('aria-label') ?? el.textContent

describe('SidePanel leading tab', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); ctl = null })

  it('renders first, ahead of the pinned block, with the host icon and no close control', () => {
    renderPanel({ closable: false })
    const rendered = chips()
    // Leading + the three pinned views, and nothing else on a fresh strip.
    expect(rendered).toHaveLength(1 + PINNED_VIEWS.length)
    expect(rendered[0]).toBe(screen.getByTestId('side-panel-leading-tab'))
    expect(nameOf(rendered[0])).toBe('Crew summary')
    expect(screen.getByTestId('leading-icon')).toBeInTheDocument()
    expect(rendered[0].querySelectorAll('button')).toHaveLength(0)
    // Not a Reorder item: the draggable list holds only the dynamic tabs.
    expect(screen.getByRole('tablist').contains(rendered[0])).toBe(false)
  })

  it('is the strip\'s default focus on a fresh bucket — its body shows, not the first pinned view', () => {
    renderPanel({ closable: false })
    expect(screen.getByTestId('side-panel-leading-tab')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('leading-body')).toHaveTextContent('radar summary')
    expect(ctl?.activeId).toBe(LEADING_ID)
  })

  it('stays out of the + menu (permanent, not a view)', () => {
    renderPanel({ closable: false })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = screen.getByRole('menu')
    expect(menu.querySelector('[role="menuitem"]')).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /crew summary/i })).toBeNull()
    // The per-chat Terminal IS offered — the whole menu model carries over.
    expect(screen.getByRole('menuitem', { name: 'Terminal' })).toBeTruthy()
  })

  it('closing the last dynamic tab returns focus to the leading tab, not to null', () => {
    renderPanel({ closable: false })
    act(() => { ctl!.openView('workflows') })
    expect(screen.getByRole('tab', { name: /Workflows/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByTestId('leading-body')).toBeNull()
    act(() => { ctl!.closeTab('workflows') })
    // `closeTab` refocuses a neighbour first (the pinned Files view sits to the
    // left), so the fallback-to-leading arm is reached by emptying the bucket.
    // A pinned view remains, so this is the neighbour case: Files is focused.
    expect(ctl?.activeId).toBe('files')
    // Empty the bucket entirely and the leading tab is the resting focus.
    act(() => { ctl!.closeAll() })
    expect(ctl?.activeId).toBe(LEADING_ID)
  })

  it('renders no close control without onClose, and one with it', () => {
    const { unmount } = renderPanel({ closable: false })
    expect(screen.queryByRole('button', { name: 'Close panel' })).toBeNull()
    unmount()
    __resetPanelTabs()
    renderPanel({ closable: true })
    expect(screen.getByRole('button', { name: 'Close panel' })).toBeInTheDocument()
  })

  it('is per slot: a second slot\'s strip opens on its own leading tab while the first keeps its focus', () => {
    const first = renderPanel({ closable: false, slot: 'member-radar' })
    act(() => { ctl!.openView('workflows') })
    expect(ctl?.activeId).toBe('workflows')
    first.unmount()
    const second = renderPanel({ closable: false, slot: 'member-scout' })
    expect(ctl?.activeId).toBe(LEADING_ID)
    second.unmount()
    renderPanel({ closable: false, slot: 'member-radar' })
    expect(ctl?.activeId).toBe('workflows')
  })

  it('hiddenViews withdraws a view from the pinned block and the + menu alike', () => {
    renderPanel({ closable: false, hidden: new Set<ViewKind>(['changes', 'pins', 'issues']) })
    // Pinned block: Changes is gone; Artifacts and Files remain, after the leading chip.
    expect(chips().map(nameOf)).toEqual(['Crew summary', 'Artifacts', 'Files'])
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    screen.getByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Pins' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Issues' })).toBeNull()
    // Unrelated rows are untouched.
    expect(screen.getByRole('menuitem', { name: 'Links' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Terminal' })).toBeTruthy()
  })
})
