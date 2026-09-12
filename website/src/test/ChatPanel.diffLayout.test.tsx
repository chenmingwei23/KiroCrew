import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const BASE_DASH = {
  restore_sessions: false,
  restore_window_minutes: 30,
  merge_queued_messages: false,
  widget_density: 'more' as const,
  verbosity: 'default' as const,
  quick_send: false,
  session_grid: false,
  tail_fork_enabled: false,
  link_previews: false,
}

const { updateDashboardConfigMock, patchConfigMock } = vi.hoisted(() => ({
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
  patchConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ ...BASE_DASH }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: updateDashboardConfigMock,
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    // Downloads OFF so the feature-video readout renders no button that these
    // switch queries could reach.
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('ChatPanel settings -- Side-by-side diffs toggle', () => {
  beforeEach(() => {
    updateDashboardConfigMock.mockClear()
    patchConfigMock.mockClear()
    localStorage.clear()
  })

  it('renders in the Messages section, on by default', async () => {
    wrap(<ChatPanel />)
    // Sits beside Plain diffs, the control it shares a surface with.
    expect(await screen.findByRole('switch', { name: 'Plain diffs' })).toBeInTheDocument()
    const toggle = await screen.findByRole('switch', { name: 'Side-by-side diffs' })
    // Side-by-side is the shipped default of the shared preference.
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('writes the key the diff surfaces read as their initial layout', async () => {
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Side-by-side diffs' })
    // Turning it off switches every diff surface's initial layout to unified.
    fireEvent.click(toggle)
    // DiffBlock, FileChangeChips and SidePanel read `mc-diff-split` through
    // useDiffSplit; nothing on the server mediates between them.
    await waitFor(() => expect(localStorage.getItem('mc-diff-split')).toBe('0'))
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('seeds from the stored preference', async () => {
    localStorage.setItem('mc-diff-split', '0')
    wrap(<ChatPanel />)
    expect(await screen.findByRole('switch', { name: 'Side-by-side diffs' })).toHaveAttribute('aria-checked', 'false')
  })

  it('writes nothing to the server, like Plain diffs beside it', async () => {
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Side-by-side diffs' })
    fireEvent.click(toggle)
    await waitFor(() => expect(localStorage.getItem('mc-diff-split')).toBe('0'))
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('is disabled while plain diffs is on, since that mode drops the split control', async () => {
    localStorage.setItem('mc-diff-plain', '1')
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Side-by-side diffs' })
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    // The hint explains WHY it is unavailable, and is tied to the switch so a
    // screen reader announces it rather than leaving a greyed control unexplained.
    const hintId = toggle.getAttribute('aria-describedby')
    expect(hintId).toBe('diff-layout-plain-hint')
    const hint = document.getElementById(hintId as string)
    expect(hint).not.toBeNull()
    expect(hint?.textContent).toMatch(/Plain diffs/)
  })

  it('shows no disabled hint while plain diffs is off', async () => {
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Side-by-side diffs' })
    expect(toggle).not.toHaveAttribute('aria-disabled', 'true')
    expect(document.getElementById('diff-layout-plain-hint')).toBeNull()
  })
})
