/**
 * ChatSidebar → a plain create leaves Split View (#21935).
 *
 * A session-row click reports through `onSelectSlot`, which the chat page
 * answers by leaving Split View and showing the clicked session. The sidebar's
 * CREATE paths activated the new session (URL + window title name it) but never
 * fired `onSelectSlot`, so the grid kept drawing the old split over a session
 * that was already active. These pin the fixed contract:
 *
 *   (1) a plain header-New click fires `onSelectSlot` with the new key (so the
 *       host leaves Split View) and still activates the new session;
 *   (2) a plain folder "New chat" fires it too;
 *   (3) New incognito / New temporary fire it;
 *   (4) the open-as-a-background-tab gesture (Ctrl/Cmd/middle-click) does NOT
 *       fire it — that create keeps the user where they are, so the split must
 *       stay (and the store's active slot must not move).
 *
 * The real `createSlot` thunk runs against a mocked `api.createChatSlot`, so the
 * active-slot assertions read the thunk's ACTUAL effect off the store.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot, ChatFolder } from '../types'

// IS_MAC is frozen at module load; read the platform through a mutable getter,
// the same shape ChatSidebar.newChatInTab.test uses.
const platform = vi.hoisted(() => ({ mac: false }))
vi.mock('../hooks/useKeyboardShortcuts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useKeyboardShortcuts')>()
  return { ...actual, get IS_MAC() { return platform.mac } }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), chatFolders: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const ORIGIN = 's1'
const NEW_KEY = 'chat-new-1'
const FOLDER: ChatFolder = { id: 'f1', name: 'Work', parent_id: null, collapsed: false } as unknown as ChatFolder
const SLOTS = [{ key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' } as ChatSlot]

function renderSidebar(opts: {
  onSelectSlot?: (key: string) => void
  onOpenSlotInNewTab?: (key: string, o?: { background?: boolean }) => void
  folders?: ChatFolder[]
} = {}) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: true, slots: SLOTS, slotsLoaded: true,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: ORIGIN, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {},
      voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], opts.folders ?? [])
  // The sidebar refetches folders on mount; answer with the same list so the
  // seeded folder is not overwritten by the Proxy's default empty resolve.
  mocks.chatFolders.mockResolvedValue(opts.folders ?? [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={ORIGIN} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
              onSelectSlot={opts.onSelectSlot}
              onOpenSlotInNewTab={opts.onOpenSlotInNewTab}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

function newButton(): HTMLElement {
  return screen.getByRole('button', { name: /new chat session/i })
}

/** A middle press produces `auxclick`, not `click`. */
function middleClick(el: HTMLElement) {
  fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }))
}

async function created() {
  await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
}

describe('ChatSidebar – a plain create leaves Split View (#21935)', () => {
  beforeEach(() => {
    localStorage.clear()
    platform.mac = false
    mocks.createChatSlot.mockReset().mockResolvedValue({ key: NEW_KEY })
    mocks.chatFolders.mockReset().mockResolvedValue([])
  })

  it('plain header-New click fires onSelectSlot with the new key and activates it', async () => {
    const onSelectSlot = vi.fn()
    const store = renderSidebar({ onSelectSlot })
    fireEvent.click(newButton())
    await created()
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith(NEW_KEY))
    expect(store.getState().chat.activeSlot).toBe(NEW_KEY)
  })

  it('New incognito fires onSelectSlot with the new key', async () => {
    const onSelectSlot = vi.fn()
    renderSidebar({ onSelectSlot })
    // Open the create caret menu, then pick New incognito chat.
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('new-incognito-chat'))
    await created()
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith(NEW_KEY))
  })

  it('New temporary fires onSelectSlot with the new key', async () => {
    const onSelectSlot = vi.fn()
    renderSidebar({ onSelectSlot })
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('new-temporary-chat'))
    await created()
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith(NEW_KEY))
  })

  it('a plain folder "New chat" fires onSelectSlot with the new key', async () => {
    const onSelectSlot = vi.fn()
    renderSidebar({ onSelectSlot, folders: [FOLDER] })
    // The folder header's quick-create + button.
    fireEvent.click(await screen.findByTestId('folder-new-chat-f1'))
    await created()
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith(NEW_KEY))
  })

  it('Ctrl-click New (background tab) does NOT leave the split and keeps the user put', async () => {
    const onSelectSlot = vi.fn()
    const onOpen = vi.fn()
    const store = renderSidebar({ onSelectSlot, onOpenSlotInNewTab: onOpen })
    fireEvent.click(newButton(), { ctrlKey: true })
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('middle-click New (background tab) does NOT leave the split', async () => {
    const onSelectSlot = vi.fn()
    const onOpen = vi.fn()
    const store = renderSidebar({ onSelectSlot, onOpenSlotInNewTab: onOpen })
    middleClick(newButton())
    await created()
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(NEW_KEY, { background: true }))
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })
})
