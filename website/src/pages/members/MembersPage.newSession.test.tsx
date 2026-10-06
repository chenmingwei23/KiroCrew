import { describe, it, expect, vi, beforeEach } from 'vitest'
import { PREVIEW_DASHBOARD } from '../../utils/previewFlags'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider } from '../../components/NavigationLeaveGuard'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'

/* "New session" on the crewmate's Profile card (#16339).
 *
 * The crewmates view's Sessions tab is READ-ONLY — it lists the worker sessions
 * the crewmate is driving — and before this there was no control to START a
 * session with the crewmate: the only way to get one was for an existing
 * session to call `session_create`. The card's Sessions tab now carries a
 * "New session" action that mints a fresh chat slot bound to this member's
 * identity (the member namespace) and jumps to it on the chat page. These cases
 * pin: the control exists in the Sessions tab, it creates with the member's
 * name and `agent_kind: 'member'`, and on success it navigates to `/chat`.
 */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberProjections: vi.fn(() => Promise.resolve({ asOfSeq: 0, values: {} })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: 'kirocrew' })),
    models: vi.fn(() => Promise.resolve([])),
    agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    workspaces: vi.fn(() => Promise.resolve({ workspaces: [] })),
    kirocrewConfig: vi.fn(() => Promise.resolve({ agents: {} })),
    // The create the "New session" control drives, and the fire-and-forget
    // color apply its thunk makes right after.
    createChatSlot: vi.fn(() => Promise.resolve({ key: 'chat-new', title: '', agent: 'oncall' })),
    setSlotColor: vi.fn(() => Promise.resolve({})),
    setSlotColorHex: vi.fn(() => Promise.resolve({})),
    chatSlotProject: vi.fn(() => Promise.resolve({})),
    deleteChatSlot: vi.fn(() => Promise.resolve({})),
  },
}))

vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const WIDE_WINDOW = 1440

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

function mockRoster(members: Array<Record<string, unknown>>) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members, default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: members.find((m) => m.slug === slug)?.name ?? slug, created: false }),
  )
}

/** Open `name`'s thread, then the Profile card, then its Sessions tab. */
async function openSessionsTab(name = 'oncall') {
  mockRoster([row({ name, slug: name, slot_key: `member-${name}` })])
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <MembersPage />
    </NavigationLeaveGuardProvider>,
  )
  fireEvent.click(await screen.findByText(name))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${name}`))
  fireEvent.click(await screen.findByTestId('member-identity-pill'))
  const card = await screen.findByTestId('crew-profile-panel')
  fireEvent.click(within(card).getByRole('tab', { name: 'Sessions' }))
  return card
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  localStorage.setItem(PREVIEW_DASHBOARD, '1')
  __resetPanelTabs()
  setWindowWidth(WIDE_WINDOW)
})

describe('MembersPage Profile card — New session', () => {
  it('the Sessions tab carries a "New session" control', async () => {
    await openSessionsTab()
    const btn = await screen.findByTestId('crew-profile-new-session')
    expect(btn).toHaveTextContent('New session')
  })

  it('creates a slot bound to THIS member (member namespace) and jumps to the chat page', async () => {
    await openSessionsTab('oncall')
    fireEvent.click(await screen.findByTestId('crew-profile-new-session'))

    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    // Signature: (name, agent, model, mode, memory_mode, title, artifact,
    // folder_id, instance_id, adopt_remote_slot, agent_kind) — 11 positional
    // params, so `agent` is index 1 and `agent_kind` is index 10. The member's
    // name rides `agent` and the `member` namespace rides `agent_kind`.
    const call = (api.createChatSlot as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[1]).toBe('oncall')
    expect(call[10]).toBe('member')

    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat'))
  })

  it('shows an error and does not navigate when the create fails', async () => {
    ;(api.createChatSlot as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('backend refused'))
    await openSessionsTab('oncall')
    fireEvent.click(await screen.findByTestId('crew-profile-new-session'))

    const notice = await screen.findByTestId('crew-profile-new-session-error')
    expect(notice).toHaveTextContent('backend refused')
    expect(navigateSpy).not.toHaveBeenCalled()
  })
})
