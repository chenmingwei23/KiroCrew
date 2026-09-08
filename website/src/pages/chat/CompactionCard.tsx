import { memo, useId } from 'react'
import { Archive, ChevronRight } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import NoticeCard from './NoticeCard'
import { useRowDisclosure } from './rowDisclosure'
import type { ChatMessage } from '../../types'

/**
 * The gateway appends every compaction outcome as an ASSISTANT-role row tagged
 * `kind="compaction"` (chat_utils._append_compaction_notice), and the text it
 * writes is one of a closed set of shapes:
 *
 *   ✅ Conversation compacted: <summary>     kiro-cli's /compaction/status
 *   ✅ Conversation compacted.               no summary (Claude backend, manual)
 *   ❌ Compaction failed: <reason>           first failures of a streak
 *   ❌ Compaction has failed Nx in a row …   the streak's collapsed notice
 *   ⚠️ Compaction timed out.                 the manual /compact wait
 *
 * `summary` is the backend's whole context summary — on kiro-cli a structured
 * multi-kilobyte document (Goal / Status / Technical / Decisions …). Drawn as a
 * bubble it reads as a reply the assistant never made and pushes the real
 * conversation off-screen, so this card folds it behind a one-line header.
 *
 * Parsed on the frontend, at render time, so no history row needs rewriting:
 * a transcript persisted by any gateway since the notice existed gets the card.
 */
export type CompactionStatus = 'completed' | 'failed'

export interface ParsedCompaction {
  status: CompactionStatus
  /** The context summary the backend shipped; empty when it shipped none. */
  summary: string
  /** Failure copy with the severity glyph stripped (the card carries severity). */
  reason: string
}

// `✅` is U+2705 (no variation selector in practice, tolerate one). The colon
// is optional: the no-summary form ends in a period instead.
const COMPLETED_RE = /^\s*\u2705\uFE0F*\s*Conversation compacted\s*[:.]?\s*/u
// Failure/timeout notices lead with ❌ (U+274C) or ⚠️ (U+26A0). Only the ❌ is
// stripped here: NoticeCard already parses ⚠️ into its warn tone.
const FAILED_LEAD_RE = /^\s*\u274C\uFE0F*\s*/u

export function parseCompactionNotice(content: string): ParsedCompaction {
  const raw = content ?? ''
  const done = COMPLETED_RE.exec(raw)
  if (done) return { status: 'completed', summary: raw.slice(done[0].length).trim(), reason: '' }
  return { status: 'failed', summary: '', reason: raw.replace(FAILED_LEAD_RE, '') }
}

/** Shared predicate: the tag lives on `kind` for a row that arrived live over
 *  the websocket and on `meta.kind` for one reloaded from disk (the same split
 *  systemNotice.ts / completedTurns.ts already read). */
export function isCompactionNotice(m: Pick<ChatMessage, 'role' | 'kind' | 'meta'>): boolean {
  return m.role === 'assistant' && (m.kind ?? (m.meta?.kind as string | undefined)) === 'compaction'
}

/**
 * Collapsed system card for a compaction notice.
 *
 * Shares NoticeCard / RecoveryCard's visual grammar — same ring, background,
 * radius, px-3/py-2 step, 13px/leading-5 type and 13px lucide icon — so the
 * three gateway-authored rows read as one family in the transcript. A
 * completed compaction is a routine event, so the header is muted and the
 * summary is folded behind a chevron; expanding it renders the summary as
 * markdown inside a capped, internally scrolling region so a 20 KB summary
 * cannot take the viewport. No chevron and no button when there is nothing to
 * expand. A failure is not folded: it is drawn on NoticeCard at warn severity,
 * because the reason is the part the user acts on.
 *
 * Expansion is per-row disclosure state, not persisted (same as RecoveryCard).
 */
export default memo(function CompactionCard({ content, disclosureKey }: { content: string; disclosureKey?: string }) {
  // memo() boundary rendering i18nT() strings: subscribe so a language switch repaints.
  useLanguageGeneration()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const headlineId = useId()
  const parsed = parseCompactionNotice(content)

  if (parsed.status === 'failed') {
    return <NoticeCard content={parsed.reason} tone="warn" />
  }

  const title = i18nT('pages.chat.compactionCard.title')
  const hasSummary = parsed.summary.length > 0
  const header = (
    <>
      {hasSummary && (
        <ChevronRight
          size={13}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
      )}
      <Archive size={13} className="lucide-inline shrink-0" aria-hidden="true" />
      <span id={headlineId} className="font-medium text-text shrink-0">
        {title}
      </span>
      {hasSummary && (
        <span className="truncate text-[12px] leading-5 opacity-75 min-w-0">
          {i18nT('pages.chat.compactionCard.summary_hint')}
        </span>
      )}
    </>
  )

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="compaction-card"
      data-status={parsed.status}
      data-expanded={hasSummary ? expanded : undefined}
    >
      {hasSummary ? (
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          // No aria-label: the inner text names the button (title + hint), and
          // aria-expanded carries the toggle state — same reasoning as RecoveryCard.
          className="w-full flex items-center gap-2 px-3 py-2 min-w-0 text-left text-[13px] leading-5 hover:text-text transition-colors"
          data-testid="compaction-card-toggle"
        >
          {header}
        </button>
      ) : (
        <div className="flex items-center gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">{header}</div>
      )}
      {hasSummary && expanded && (
        // max-h + overflow-y-auto: the summary is the backend's whole context
        // digest, routinely taller than the viewport; it scrolls internally so
        // its height cannot displace the rows below. overflow-x-hidden is
        // explicit because a non-visible y-axis computes x's `visible` to
        // `auto`. tabIndex + region: a scroll region with no focusable
        // descendant is unreachable to a keyboard. Ring INSET because the body
        // is flush with the card's clipped edges (see SubagentCompletionCard).
        <div
          className="px-3 pb-3 pt-2 text-[13px] leading-5 border-t border-border max-h-[24rem] overflow-y-auto overflow-x-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
          data-testid="compaction-card-body"
          role="region"
          aria-labelledby={headlineId}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <MarkdownRenderer content={parsed.summary} />
        </div>
      )}
    </div>
  )
})
