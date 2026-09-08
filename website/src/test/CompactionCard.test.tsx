/**
 * CompactionCard — the folded system card for a `kind="compaction"` row.
 *
 * Pins three things: the notice parser (the closed set of shapes the gateway
 * writes), the card's collapsed/expanded/failed/empty states, and that BOTH
 * registries — the dashboard row set and the store-free SDK defaults — resolve
 * a compaction row to this card rather than to the assistant bubble. The
 * registry half is the bug this file exists for: the row set had no entry, so
 * the bubble fallback painted the backend's whole context summary as a reply
 * on the main chat and in every Crew DM pane.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CompactionCard, { isCompactionNotice, parseCompactionNotice } from '../pages/chat/CompactionCard'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'

const SUMMARY = '## Goal\nShip the compaction card.\n\n## Status\n- renderer registered\n- i18n filled'
const COMPLETED = `\u2705 Conversation compacted: ${SUMMARY}`

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: children => children,
  row: children => children,
  ...over,
})

describe('parseCompactionNotice', () => {
  it('splits the completed notice into status + summary, dropping prefix and colon', () => {
    expect(parseCompactionNotice(COMPLETED)).toEqual({ status: 'completed', summary: SUMMARY, reason: '' })
  })

  it('treats the no-summary form (trailing period) as completed with an empty summary', () => {
    expect(parseCompactionNotice('\u2705 Conversation compacted.')).toEqual({
      status: 'completed',
      summary: '',
      reason: '',
    })
    // A stray whitespace-only tail is not a summary either.
    expect(parseCompactionNotice('\u2705 Conversation compacted:   ').summary).toBe('')
  })

  it('classifies a ❌ notice as failed and strips only the glyph', () => {
    expect(parseCompactionNotice('\u274C Compaction failed: context too large')).toEqual({
      status: 'failed',
      summary: '',
      reason: 'Compaction failed: context too large',
    })
    expect(parseCompactionNotice('\u274C Compaction has failed 3x in a row (boom) — consider `/compact`.').reason).toMatch(
      /^Compaction has failed 3x/,
    )
  })

  it('leaves a ⚠️ timeout notice intact for NoticeCard to classify', () => {
    const s = '\u26A0\uFE0F Compaction timed out.'
    expect(parseCompactionNotice(s)).toEqual({ status: 'failed', summary: '', reason: s })
  })
})

describe('isCompactionNotice', () => {
  it('matches the live-websocket tag (kind) and the reloaded tag (meta.kind)', () => {
    expect(isCompactionNotice(msg('assistant', { kind: 'compaction' }))).toBe(true)
    expect(isCompactionNotice(msg('assistant', { meta: { kind: 'compaction' } }))).toBe(true)
  })

  it('never matches an untagged assistant row or a tagged non-assistant row', () => {
    expect(isCompactionNotice(msg('assistant', { content: COMPLETED }))).toBe(false)
    expect(isCompactionNotice(msg('notice', { kind: 'compaction' }))).toBe(false)
    expect(isCompactionNotice(msg('assistant', { kind: 'session_reload' }))).toBe(false)
  })
})

describe('CompactionCard', () => {
  it('renders collapsed by default: title visible, summary not rendered', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const card = container.querySelector('[data-testid="compaction-card"]')!
    expect(card.getAttribute('data-status')).toBe('completed')
    expect(card.getAttribute('data-expanded')).toBe('false')
    expect(screen.getByText('Context compacted')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="compaction-card-body"]')).toBeNull()
    expect(container.textContent).not.toContain('Ship the compaction card')
    // The ✅ is the card's job now, not the copy's.
    expect(container.textContent).not.toContain('\u2705')
    expect(container.textContent).not.toContain('Conversation compacted')
  })

  it('expands on click to a markdown body and collapses again', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const toggle = screen.getByTestId('compaction-card-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const body = container.querySelector('[data-testid="compaction-card-body"]')!
    expect(body).not.toBeNull()
    // Markdown, not raw text: the `## Goal` heading becomes an h2.
    expect(body.querySelector('h2')?.textContent).toBe('Goal')
    expect(body.textContent).toContain('Ship the compaction card.')
    // The body is a capped, internally scrolling, keyboard-reachable region.
    expect(body.classList.contains('overflow-y-auto')).toBe(true)
    expect(body.className).toMatch(/max-h-\[/)
    expect(body.getAttribute('role')).toBe('region')
    expect(body.getAttribute('tabindex')).toBe('0')
    fireEvent.click(toggle)
    expect(container.querySelector('[data-testid="compaction-card-body"]')).toBeNull()
  })

  it('draws no chevron and no button when there is no summary to expand', () => {
    const { container } = render(<CompactionCard content={'\u2705 Conversation compacted.'} />)
    expect(screen.getByText('Context compacted')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="compaction-card-toggle"]')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('svg.lucide-chevron-right')).toBeNull()
    expect(container.querySelector('[data-testid="compaction-card"]')!.hasAttribute('data-expanded')).toBe(false)
  })

  it('renders a failure on NoticeCard at warn severity, unfolded, with the reason visible', () => {
    const { container } = render(<CompactionCard content={'\u274C Compaction failed: context too large'} />)
    expect(container.querySelector('[data-testid="compaction-card"]')).toBeNull()
    const notice = container.querySelector('[data-testid="notice-card"]')!
    expect(notice).not.toBeNull()
    expect(notice.getAttribute('data-tone')).toBe('warn')
    expect(container.querySelector('svg.lucide-triangle-alert')).not.toBeNull()
    expect(screen.getByText('Compaction failed: context too large')).toBeInTheDocument()
    expect(container.textContent).not.toContain('\u274C')
    expect(container.querySelector('button')).toBeNull()
  })

  it('shares the NoticeCard / RecoveryCard chrome so the three rows read as one family', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const card = container.querySelector('[data-testid="compaction-card"]')!
    for (const cls of ['self-center', 'w-full', 'rounded-md', 'ring-1', 'ring-inset', 'ring-border', 'bg-card', 'text-muted']) {
      expect(card.classList.contains(cls)).toBe(true)
    }
    const row = card.firstElementChild!
    for (const cls of ['px-3', 'py-2', 'text-[13px]', 'leading-5', 'gap-2']) {
      expect(row.classList.contains(cls)).toBe(true)
    }
  })
})

describe('registry resolution', () => {
  const live = msg('assistant', { content: COMPLETED, kind: 'compaction' })
  const reloaded = msg('assistant', { content: COMPLETED, meta: { kind: 'compaction' } })

  it('the dashboard row set (ChatPage + ChatPane) resolves a compaction row ahead of the bubble', () => {
    const registry = mergeRenderers(createTranscriptRenderers({ slot: 's1' }))
    expect(resolveRenderer(live, registry)?.id).toBe('compaction')
    expect(resolveRenderer(reloaded, registry)?.id).toBe('compaction')
    // Ordinary assistant rows still take the assistant entry.
    expect(resolveRenderer(msg('assistant', { content: 'hi' }), registry)?.id).toBe('assistant')
  })

  it('the SDK defaults (ChatEmbed / SideChat) resolve it too, not the assistant bubble', () => {
    expect(resolveRenderer(live, defaultMessageRenderers)?.id).toBe('compaction')
    expect(resolveRenderer(reloaded, defaultMessageRenderers)?.id).toBe('compaction')
  })

  it('a host entry that claims assistant with no match (ChatPage bubble) does not outrank it', () => {
    // Mirrors ChatPage.mergeRenderers([...shared, ..., bubble]).
    const bubble = { id: 'bubble', roles: ['user', 'assistant', 'streaming', 'inject'], render: () => null }
    const registry = mergeRenderers([...createTranscriptRenderers({ slot: 's1' }), bubble])
    expect(resolveRenderer(live, registry)?.id).toBe('compaction')
  })

  it('renders the card, not markdown prose, through the row set', () => {
    const registry = mergeRenderers(createTranscriptRenderers({ slot: 's1' }))
    const entry = resolveRenderer(live, registry)!
    const { container } = render(<>{entry.render(live, ctx({ messages: [live] }))}</>)
    expect(container.querySelector('[data-testid="compaction-card"]')).not.toBeNull()
    expect(container.textContent).not.toContain('Ship the compaction card')
  })
})
